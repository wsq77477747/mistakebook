import fs from "node:fs";

const html = fs.readFileSync("assets/template.html", "utf8");
const match = html.match(/<script>([\s\S]*?)<\/script>/);
if (!match) throw new Error("script block missing");
if (/\{\{(?:DATA|MAIN|NAV|NAVQ|STATS|TOTAL)\}\}/.test(html)) {
  throw new Error("legacy data placeholder remains in the authenticated app shell");
}
for (const marker of [
  'id="ai-voice"',
  'id="ai-voice-status"',
  'window.SpeechRecognition||window.webkitSpeechRecognition',
  "voiceRecognition.lang='zh-CN'",
  "voiceRecognition.interimResults=true",
  'id="review-skip"',
  "未记录掌握情况",
  "下次复习：",
  "rl.schedule&&rl.schedule.next_review_at",
  'class="qcard-actions"',
  'aria-label="编辑此题"',
  'aria-label="删除此题"',
  'header{position:static;top:auto;z-index:auto',
  'z-index:130',
  '.table-scroll{',
  'id="add-subject"',
  'id="pv-subject"',
  'id="list-subject-filter"',
  "subject_hint:document.getElementById('add-subject').value",
  'AI 服务暂时不可用',
  'grid-template-columns:repeat(4,minmax(0,1fr))',
]) {
  if (!html.includes(marker)) throw new Error(`voice input marker missing: ${marker}`);
}
new Function(match[1]);
console.log("frontend script syntax OK");
