// ============================================================
// 模拟后端：以下逻辑全部在前端 JS 里假扮服务器返回。
// 你以后学后端时，只要把 MockBackend 里每个方法的实现
// 换成真实的 fetch('https://你的服务器/接口') 即可，
// 上面的界面代码完全不用动。
// ============================================================
const MockBackend = {
  // 登录：真实后端会校验账号密码，这里只演示流程
  login(username, password) {
    return new Promise(function (resolve) {
      setTimeout(function () {
        if (username && password) {
          resolve({ ok: true, user: username });
        } else {
          resolve({ ok: false, msg: "用户名和密码都不能为空" });
        }
      }, 400);
    });
  },

  // 提交截图：真实后端会接收图片、识别版本、判定状态。
  // 这里模拟“先等待，再返回最终结果”的过程。
  submitScreenshot(file) {
    return new Promise(function (resolve) {
      // 真实场景：这一步是上传图片到服务器（fetch + FormData）
      // 提交后服务器通常不会立刻给出最终结论，
      // 常见做法是前端轮询(POLL)或服务器推送(WebSocket)拿状态。
      // 这里用延时直接给出“最终状态”来演示三种结果。
      setTimeout(function () {
        // 演示：50% 成功，50% 异常（需再次提交）
        var success = Math.random() > 0.5;
        if (success) {
          resolve({ status: "success" }); // 提交成功
        } else {
          resolve({ status: "error" });   // 异常（需再次提交）
        }
      }, 2500);
    });
  },
};

// ===================== 以下是界面逻辑（前端） =====================
var loginView = document.getElementById("login-view");
var mainView = document.getElementById("main-view");
var loginBtn = document.getElementById("login-btn");
var loginMsg = document.getElementById("login-msg");
var fileInput = document.getElementById("file-input");
var preview = document.getElementById("preview");
var previewImg = document.getElementById("preview-img");
var submitBtn = document.getElementById("submit-btn");
var statusEl = document.getElementById("status");
var statusTip = document.getElementById("status-tip");
var resubmitBtn = document.getElementById("resubmit-btn");

// ---- 步骤一：登录 ----
loginBtn.addEventListener("click", function () {
  var username = document.getElementById("username").value.trim();
  var password = document.getElementById("password").value;
  loginMsg.textContent = "登录中…";
  MockBackend.login(username, password).then(function (res) {
    if (res.ok) {
      loginView.classList.add("hidden");
      mainView.classList.remove("hidden");
    } else {
      loginMsg.textContent = res.msg;
    }
  });
});

// ---- 步骤一（续）：选择图片后预览，并允许提交 ----
fileInput.addEventListener("change", function () {
  var file = fileInput.files[0];
  if (!file) {
    preview.classList.add("hidden");
    submitBtn.disabled = true;
    return;
  }
  var reader = new FileReader();
  reader.onload = function (e) {
    previewImg.src = e.target.result;
    preview.classList.remove("hidden");
    submitBtn.disabled = false;
  };
  reader.readAsDataURL(file);
});

// ---- 步骤二 + 三：确认提交，并展示网页传回的状态 ----
submitBtn.addEventListener("click", function () {
  var file = fileInput.files[0];
  if (!file) return;

  submitBtn.disabled = true;
  resubmitBtn.classList.add("hidden");
  setStatus("waiting", "等待中…");

  MockBackend.submitScreenshot(file).then(function (res) {
    if (res.status === "success") {
      setStatus("success", "提交成功");
      statusTip.textContent = "服务器已确认截图版本，问题已改进。";
    } else {
      setStatus("error", "异常（需再次提交）");
      statusTip.textContent = "服务器识别异常，请重新上传截图。";
      resubmitBtn.classList.remove("hidden");
    }
  });
});

// ---- 异常时：再次上传 ----
resubmitBtn.addEventListener("click", function () {
  fileInput.value = "";
  preview.classList.add("hidden");
  submitBtn.disabled = true;
  resubmitBtn.classList.add("hidden");
  setStatus("idle", "尚未提交");
  statusTip.textContent = "";
});

// 小工具：统一设置状态文字和颜色
function setStatus(cls, text) {
  statusEl.className = "status " + cls;
  statusEl.textContent = text;
}
