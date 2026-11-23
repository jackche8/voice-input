/* 仅在用户点击回调里写入剪贴板，不读剪贴板。
 * writeAsyncText 优先走 ClipboardItem(Promise<Blob>) 保留 WebKit2GTK / WKWebView 用户手势激活，
 * 不支持时复用同一 textPromise 回退 writeText，绝不重复发请求且保留原始 ApiError。
 */
(function (root) {
  "use strict";

  function createClipboard(deps) {
    var impl = (deps && deps.clipboard) || null;
    var ClipboardItemCtor = (deps && deps.ClipboardItem) || (typeof ClipboardItem === "function" ? ClipboardItem : null);
    var BlobCtor = (deps && deps.Blob) || (typeof Blob === "function" ? Blob : null);

    function resolveClip() {
      if (impl) return impl;
      if (typeof navigator !== "undefined") return navigator.clipboard;
      return null;
    }

    function writeText(text) {
      var clip = resolveClip();
      if (!clip || typeof clip.writeText !== "function") {
        return Promise.reject(new Error("当前环境不能写入剪贴板"));
      }
      return Promise.resolve(clip.writeText(text == null ? "" : String(text)));
    }

    function writeAsyncText(getText) {
      var clip = resolveClip();
      if (!clip || (typeof clip.write !== "function" && typeof clip.writeText !== "function")) {
        return Promise.reject(new Error("当前环境不能写入剪贴板"));
      }
      if (typeof getText !== "function") {
        return Promise.reject(new Error("缺少异步复制源"));
      }

      var hasTextError = false;
      var textError = null;
      var rawPromise;
      try {
        rawPromise = Promise.resolve(getText());
      } catch (syncErr) {
        return Promise.reject(syncErr);
      }

      var textPromise = rawPromise.then(
        function (val) { return val == null ? "" : String(val); },
        function (err) {
          hasTextError = true;
          textError = err;
          throw err;
        }
      );
      /* 抑制派生分支在 clip.write 提前失败时触发未处理拒绝；真正错误仍通过返回 Promise 抛出。 */
      textPromise.then(undefined, function () {});

      if (typeof clip.write === "function" && ClipboardItemCtor && BlobCtor) {
        var blobPromise = textPromise.then(function (str) {
          return new BlobCtor([str], { type: "text/plain" });
        });
        blobPromise.then(undefined, function () {});

        var item;
        try {
          item = new ClipboardItemCtor({ "text/plain": blobPromise });
        } catch (ctorErr) {
          return textPromise.then(function (str) {
            return writeText(str);
          });
        }
        return Promise.resolve(clip.write([item])).then(
          function () {
            if (hasTextError) throw textError;
          },
          function (writeErr) {
            if (hasTextError) throw textError;
            return textPromise.then(
              function (str) {
                if (typeof clip.writeText === "function") {
                  return Promise.resolve(clip.writeText(str)).then(
                    function () {},
                    function () { throw writeErr; }
                  );
                }
                throw writeErr;
              },
              function (origErr) {
                throw origErr;
              }
            );
          }
        );
      }

      return textPromise.then(function (str) {
        return writeText(str);
      });
    }

    return {
      writeText: writeText,
      writeAsyncText: writeAsyncText
    };
  }

  root.createClipboard = createClipboard;
})(window.V3Business = window.V3Business || {});
