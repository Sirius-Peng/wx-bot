// Keep native Quit (including Windows menu/OS requests) on the same cleanup route.
'use strict';
function installQuitGuard(app, quit) {
  app.on('before-quit', event => {
    if (app.isQuitting) return;
    event.preventDefault();
    quit();
  });
}
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}
function errorPage(message, detail) {
  return 'data:text/html;charset=utf-8,' + encodeURIComponent(`<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<style>body{margin:0;height:100vh;display:grid;place-items:center;background:#F4F6F8;font-family:"Microsoft YaHei",sans-serif;color:#1B1F26}.box{max-width:560px;padding:36px;background:white;border-radius:16px}code{display:block;white-space:pre-wrap;word-break:break-all;background:#F0F2F5;padding:14px}button{margin-top:18px;padding:10px 18px;border:0;border-radius:8px;background:#07C160;color:white}</style></head><body><div class="box"><h1>后台服务启动失败</h1><p>${escapeHtml(message)}</p><code>${escapeHtml(detail)}</code><button id="retry" onclick="this.disabled=true;this.textContent='正在重启…';window.wxbotDesktop.backend.restart().then(r=>{if(!r.ok){this.disabled=false;this.textContent='重试';document.querySelector('code').textContent=r.error||'请查看日志';}}).catch(()=>{this.disabled=false;this.textContent='重试';})">重试启动后端</button></div></body></html>`);
}
module.exports = { installQuitGuard, escapeHtml, errorPage };
