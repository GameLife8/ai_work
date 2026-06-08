// 剪贴板兜底 polyfill
// ============================================================================
// 现象:HTTP(非 localhost)访问时,浏览器把页面判为「非安全上下文」,
//       navigator.clipboard 为 undefined。Chainlit 的复制按钮调
//       navigator.clipboard.writeText(...) → 抛
//       "Cannot read properties of undefined (reading 'writeText')"。
// 方案:不依赖 HTTPS——用老的 document.execCommand('copy') 兜底,
//       在 navigator.clipboard.writeText 缺失时补上它。
// ============================================================================
(function () {
  function fallbackCopy(text) {
    return new Promise(function (resolve, reject) {
      try {
        var ta = document.createElement('textarea');
        ta.value = text == null ? '' : String(text);
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.top = '-9999px';
        ta.style.left = '-9999px';
        ta.style.opacity = '0';
        document.body.appendChild(ta);

        // 保存当前选区,复制完恢复,避免打断用户选中
        var sel = document.getSelection();
        var saved = sel && sel.rangeCount > 0 ? sel.getRangeAt(0) : null;

        ta.focus();
        ta.select();
        try { ta.setSelectionRange(0, ta.value.length); } catch (e) {}

        var ok = false;
        try { ok = document.execCommand('copy'); } catch (e) { ok = false; }

        document.body.removeChild(ta);
        if (saved && sel) { sel.removeAllRanges(); sel.addRange(saved); }

        ok ? resolve() : reject(new Error('execCommand copy 返回 false'));
      } catch (e) {
        reject(e);
      }
    });
  }

  function needsPolyfill() {
    return !navigator.clipboard || typeof navigator.clipboard.writeText !== 'function';
  }

  if (needsPolyfill()) {
    try {
      Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: {
          writeText: fallbackCopy,
          readText: function () {
            return Promise.reject(new Error('readText 在非安全上下文不支持'));
          },
        },
      });
    } catch (e) {
      // 个别浏览器 navigator.clipboard 是只读 getter,defineProperty 失败时直接赋值兜一手
      try {
        navigator.clipboard = { writeText: fallbackCopy };
      } catch (e2) {
        /* 实在不行就放弃,至少不再因 polyfill 自身报错 */
      }
    }
    try {
      console.info('[clipboard-polyfill] 已用 execCommand 兜底 navigator.clipboard.writeText(HTTP 非安全上下文)');
    } catch (e) {}
  }
})();
