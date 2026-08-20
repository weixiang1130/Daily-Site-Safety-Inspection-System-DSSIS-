// 熱危害風險等級對應的應辦措施
//
// 依據勞動部職業安全衛生署《高氣溫作業熱危害預防指引》
// （108.1.28 訂定，114.6.20 勞職授字第 1140252820 號第 2 次修正）
// 附表二「熱危害風險等級對應之熱指數及風險管理原則」與
// 附表三「不同熱危害風險等級對應之危害預防及管理措施表」。
//
// 為什麼要把措施寫進系統
// ----------------------
// 只顯示「第三級」對現場沒有用——值班人員未必記得第三級要做什麼。
// 牆上要直接講出「現在該做哪幾件事」，指引才會變成行動。
//
// 等級判定本身在 hazard.ts（門檻取自附表二，不可自行四捨五入）。
// 本檔只負責「這一級要做什麼」。

import { levelOf, type HazardLevel } from "./hazard.ts";

export interface HeatGuidance {
  level: HazardLevel;
  /** 法規用語，例如「第三級」。等級 0 代表未達第一級。 */
  name: string;
  range: string;
  /** 附表二的風險管理原則 */
  principle: string;
  /** 該級應採取的全部措施，取自附表三 */
  measures: string[];
  /**
   * 這一級相較前一級「多」要求的事。
   *
   * 牆上空間有限，把八項每級都要做的基本措施全列出來，反而看不出這一級
   * 的重點在哪。完整清單留給彈出提醒。
   */
  focus: string[];
  /** 現場最常用到、且指引有明確數字的操作要點 */
  reminders: string[];
}

// 附表三中各級都要做的基礎措施
const BASE_MEASURES = [
  "降低勞工暴露溫度：遮陽、風扇、水霧等降溫設施",
  "指派專人定期巡視作業情形，隨時掌握勞工健康狀況",
  "提供鄰近、遮陽且能降溫的休息場所",
  "提供淺色、寬鬆、透氣排汗之工作服與通風帽盔",
  "於作業場所提供充足飲用水及電解質",
  "適當選配作業勞工，確認其身體健康狀況",
  "實施熱危害預防教育訓練",
  "建立緊急應變處理機制與急救措施",
];

// 第二級起才增加的措施
const LEVEL2_ADD = [
  "調整勞工熱適應能力（新進人員應逐日增加熱暴露時間）",
  "調整勞工作業時間，減少連續作業，避開高氣溫時段",
];

const LEVEL3_ADD = [
  "避免使勞工於高氣溫時段從事戶外作業",
  "必要時選用個人防護器具（冰背心、風扇背心等），但這是最後一道防線",
];

const LEVEL4_ADD = [
  "避免使勞工從事戶外作業",
  "確有必要時應加強緊急應變機制",
  "依設施規則第 303 條之 1 設置遮陽、降溫設備及適當休息場所",
  "避免穿著不透氣厚重防護衣作業，避免重體力作業",
];

// 指引裡有明確數字、現場最需要被提醒的幾件事。
// 陽光直照要提升一級的規定不放這裡——它由 escalated() 另外算出實際會提升到
// 第幾級並單獨顯示，講得比一句原則更有用，兩邊都寫只是佔掉牆面。
const COMMON_REMINDERS = [
  "補充水分：每 15～20 分鐘 1 次、每次 150～200 毫升，規律執行而非口渴才喝",
  "重體力作業：每小時至少給予 20 分鐘充足休息",
];

const LEVELS: Record<HazardLevel, Omit<HeatGuidance, "level">> = {
  0: {
    name: "未達第一級",
    range: "熱指數未達 26.7",
    principle: "尚未達指引所定風險等級，維持一般作業管理即可。",
    measures: [],
    focus: [],
    reminders: [],
  },
  1: {
    name: "第一級",
    range: "熱指數 26.7 以上，未達 32.2",
    principle: "熱暴露之基本防護與原則；從事重體力作業時應提高警覺。",
    measures: BASE_MEASURES,
    focus: ["遮陽降溫、定期巡視、充足飲水與電解質等基本防護",
            "從事重體力作業時應特別提高警覺"],
    reminders: COMMON_REMINDERS,
  },
  2: {
    name: "第二級",
    range: "熱指數 32.2 以上，未達 40.6",
    principle: "實施危害預防措施及提升危害認知。",
    measures: [...BASE_MEASURES, ...LEVEL2_ADD],
    focus: LEVEL2_ADD,
    reminders: COMMON_REMINDERS,
  },
  3: {
    name: "第三級",
    range: "熱指數 40.6 以上，未達 54.4",
    principle: "強化採取之危害預防及管理措施，並注意勞工身體狀況。",
    measures: [...BASE_MEASURES, ...LEVEL2_ADD, ...LEVEL3_ADD],
    focus: LEVEL3_ADD,
    reminders: COMMON_REMINDERS,
  },
  4: {
    name: "第四級",
    range: "熱指數 54.4 以上",
    principle: "更積極執行相關防護措施，原則上避免使勞工從事戶外作業。",
    measures: [...BASE_MEASURES, ...LEVEL2_ADD, ...LEVEL3_ADD, ...LEVEL4_ADD],
    focus: LEVEL4_ADD,
    reminders: COMMON_REMINDERS,
  },
};

/** 依熱指數值取得該級的應辦措施。 */
export function heatGuidance(heatIndex: number | null | undefined): HeatGuidance | null {
  if (heatIndex == null || !Number.isFinite(heatIndex)) return null;
  const level = levelOf("heat_index", heatIndex);
  return { level, ...LEVELS[level] };
}

/**
 * 陽光直接照射或穿著不透氣防護衣時應提升的等級。
 *
 * 指引附表二備註 3 有明文規定，而工地戶外作業多屬陽光直接照射，
 * 因此把提升後的等級一併算出來顯示——只顯示現值會讓現場低估。
 * 但不直接把它當成判定結果：是否真的在陽光直照下要由現場認定，
 * 系統替現場做這個假設並不恰當。
 */
export function escalated(level: HazardLevel): HeatGuidance {
  const up = Math.min(level + 1, 4) as HazardLevel;
  return { level: up, ...LEVELS[up] };
}
