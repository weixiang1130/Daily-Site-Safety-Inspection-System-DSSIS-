// 環境量測值的危害分級
//
// 為什麼自己算
// ------------
// 廠商平台雖然有「危害等級」欄位，但實測（2026-08-19）在熱指數 49.4 的
// 情況下仍回報 0。這比沒有數據更危險——工地會誤以為現場安全。因此本系統
// 一律以自己的邏輯分級，廠商的原始值另存為 vendor_hazard_level，只作為
// 與廠商釐清問題時的佐證，不進入任何告警判斷。
//
// 分級依據都寫在下方各指標旁邊。門檻集中在這個檔案，日後法規修正或公司
// 自訂更嚴格的標準時，只需要改這裡。

/** 0 正常｜1 注意｜2 警戒｜3 危險｜4 極度危險 */
export type HazardLevel = 0 | 1 | 2 | 3 | 4;

export const LEVEL_LABEL: Record<HazardLevel, string> = {
  0: "正常", 1: "注意", 2: "警戒", 3: "危險", 4: "極度危險",
};

interface Threshold {
  label: string;
  unit: string;
  /** 由低到高的分界值；落在第 n 個區間即為第 n 級。 */
  breaks: number[];
  basis: string;
  /** 讀值本身無法直接對應法規值時的說明，會顯示在戰情室上。 */
  caveat?: string;
}

export const THRESHOLDS: Record<string, Threshold> = {
  // 美國 NWS 熱指數分級（80／90／105／130°F）。我國職安署《高氣溫戶外作業
  // 勞工熱危害預防指引》採同一套分級，區分注意、警戒、危險、極度危險四級。
  heat_index: {
    // 標示為「體感」是刻意的：熱指數是體感溫度，不是氣溫。
    // 它和「溫度」並排且單位同為 °C，不講清楚很容易被讀成「外面 42.8 度」，
    // 實際意義是「31.4 度加上 75.3% 濕度，體感相當於 42.8 度」。
    label: "熱指數 體感", unit: "°C", breaks: [27, 32, 41, 54],
    basis: "職安署高氣溫戶外作業熱危害預防指引／美國 NWS 熱指數分級",
    caveat: "體感溫度，非氣溫。由溫度與相對濕度推算，代表人體實際感受到的熱負荷",
  },

  // 職業安全衛生設施規則第 300 條：8 小時日時量平均音壓級超過 85 分貝應
  // 採取聽力保護措施，90 分貝為 8 小時容許暴露值。
  noise: {
    label: "噪音", unit: "dB", breaks: [80, 85, 90],
    basis: "職業安全衛生設施規則第 300 條",
    caveat: "為即時音壓級，非 8 小時日時量平均值，不可直接視為法規符合性判定",
  },

  // 環境部空氣品質指標（AQI）PM2.5 24 小時值分界：
  // 15.5 普通、35.5 對敏感族群不健康、54.5 對所有族群不健康。
  pm25: {
    label: "PM2.5", unit: "μg/m³", breaks: [15.5, 35.5, 54.5],
    basis: "環境部空氣品質指標 AQI（PM2.5 24 小時值）",
    caveat: "為即時濃度，AQI 分界原以 24 小時平均值定義",
  },

  // 環境部空氣品質指標（AQI）PM10 24 小時值分界。工地揚塵多屬粗顆粒，
  // PM10 明顯高於 PM2.5 時通常代表現場揚塵而非區域空品問題。
  pm10: {
    label: "PM10", unit: "μg/m³", breaks: [51, 101, 255],
    basis: "環境部空氣品質指標 AQI（PM10 24 小時值）",
    caveat: "為即時濃度，AQI 分界原以 24 小時平均值定義",
  },

  // 溫濕度不單獨分級：熱指數已經同時涵蓋兩者，且熱指數才是能直接對應到
  // 「加強休息、調整工時、停止作業」的指標。這裡只是為了讓戰情室有一致的
  // 標籤與單位可用，breaks 留空代表永遠是正常。
  temperature: {
    label: "溫度", unit: "°C", breaks: [],
    basis: "僅供參考，熱危害判定以熱指數為準",
  },
  humidity: {
    label: "濕度", unit: "%", breaks: [],
    basis: "僅供參考，熱危害判定以熱指數為準",
  },
};

/** 依門檻表判定單一指標的等級。未列管的指標一律回 0。 */
export function levelOf(metric: string, value: number): HazardLevel {
  const t = THRESHOLDS[metric];
  if (!t || !Number.isFinite(value)) return 0;
  let level = 0;
  for (const b of t.breaks) if (value >= b) level += 1;
  return Math.min(level, 4) as HazardLevel;
}

/**
 * 由溫度與相對濕度推算熱指數（NWS Rothfusz 迴歸式）。
 *
 * 廠商若停供熱指數，只要還有溫濕度就能自行算出，不受制於對方。
 * 迴歸式以華氏定義，因此內部換算後再轉回攝氏。
 *
 * @param tempC  溫度（°C）
 * @param rh     相對濕度（%）
 */
export function heatIndexC(tempC: number, rh: number): number | null {
  if (!Number.isFinite(tempC) || !Number.isFinite(rh)) return null;
  if (rh < 0 || rh > 100) return null;

  const T = tempC * 9 / 5 + 32;

  // 低溫段迴歸式誤差大，NWS 規定改用簡式，且僅在結果達 80°F 才套用完整式
  const simple = 0.5 * (T + 61 + (T - 68) * 1.2 + rh * 0.094);
  if ((simple + T) / 2 < 80) return round1((simple - 32) * 5 / 9);

  let hi =
    -42.379 + 2.04901523 * T + 10.14333127 * rh
    - 0.22475541 * T * rh - 0.00683783 * T * T - 0.05481717 * rh * rh
    + 0.00122874 * T * T * rh + 0.00085282 * T * rh * rh
    - 0.00000199 * T * T * rh * rh;

  // NWS 對乾燥與極濕兩端的修正
  if (rh < 13 && T >= 80 && T <= 112) {
    hi -= ((13 - rh) / 4) * Math.sqrt((17 - Math.abs(T - 95)) / 17);
  } else if (rh > 85 && T >= 80 && T <= 87) {
    hi += ((rh - 85) / 10) * ((87 - T) / 5);
  }

  return round1((hi - 32) * 5 / 9);
}

const round1 = (n: number) => Math.round(n * 10) / 10;

/**
 * 測站整體危害等級：取各指標中最高者。
 *
 * 取最高而非平均——任何一項達到危險都必須反映出來，
 * 不能被其他正常的指標稀釋掉。
 */
export function stationLevel(metrics: Record<string, number>): HazardLevel {
  let max: HazardLevel = 0;
  for (const [k, v] of Object.entries(metrics)) {
    const l = levelOf(k, v);
    if (l > max) max = l;
  }
  return max;
}
