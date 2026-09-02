If WScript.Arguments.Count < 1 Then WScript.Quit 1
CreateObject("WScript.Shell").Run """" & WScript.Arguments(0) & """", 0, False
