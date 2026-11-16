// 来源采信页：建包、收件、作废来源、发布版本。所有请求走同源接口。
const state = { busy: false };

function el(id) {
  return document.getElementById(id);
}

function notice(message, isError = false) {
  const box = el("notice");
  if (!box) return;
  box.textContent = message;
  box.classList.toggle("is-error", Boolean(isError));
  box.hidden = false;
}

function currentPackageId() {
  const meta = document.querySelector(".page-meta");
  const text = meta ? meta.textContent.trim() : "";
  const match = text.match(/pkg-[0-9a-f]+/);
  return match ? match[0] : null;
}

async function postJSON(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = { error: "E_BAD_RESPONSE", message: "响应不是 JSON" };
  }
  if (!response.ok) {
    const code = payload && payload.error ? payload.error : response.status;
    const detail = payload && payload.message ? payload.message : "请求失败";
    const pkg = payload && payload.detail && payload.detail.package_id;
    throw new Error(code + " " + detail + (pkg ? " · " + pkg : ""));
  }
  return payload;
}

function lock(label) {
  state.busy = true;
  const button = document.activeElement;
  if (button && button.classList.contains("btn")) {
    button.dataset.label = button.textContent;
    button.textContent = label;
    button.disabled = true;
    return button;
  }
  return null;
}

function unlock(button) {
  state.busy = false;
  if (!button) return;
  button.textContent = button.dataset.label || button.textContent;
  button.disabled = false;
}

async function withBusy(label, task) {
  if (state.busy) return;
  const button = lock(label);
  try {
    await task();
  } catch (error) {
    notice(String(error.message || error), true);
  } finally {
    unlock(button);
  }
}

function reloadAfter(message) {
  notice(message);
  window.setTimeout(() => window.location.reload(), 500);
}

function bind() {
  const create = el("create-package-button");
  if (create) {
    create.addEventListener("click", () => withBusy("建包中", async () => {
      const select = el("package-sample-select");
      const expected = [];
      if (select) {
        Array.from(select.options).forEach((option) => {
          if (!option.disabled) expected.push({ filename: option.value });
        });
      }
      const payload = await postJSON("/api/packages", { expected_sources: expected });
      window.location.href = "/packages?package_id=" + encodeURIComponent(payload.package_id);
    }));
  }

  const receiveButton = el("receive-source-button");
  if (receiveButton) {
    receiveButton.addEventListener("click", () => withBusy("收件中", async () => {
      const select = el("package-sample-select");
      const packageId = currentPackageId();
      if (!packageId) throw new Error("先建包再收件");
      if (!select) throw new Error("没有可选样例");
      const payload = await postJSON(
        "/api/packages/" + encodeURIComponent(packageId) + "/sources",
        { sample: select.value });
      const snap = payload.snapshot || {};
      reloadAfter("已收 " + (snap.filename || select.value) + " · " + (snap.status_label || snap.status));
    }));
  }

  const publish = el("publish-package-button");
  if (publish) {
    publish.addEventListener("click", () => withBusy("发布中", async () => {
      const packageId = currentPackageId();
      if (!packageId) throw new Error("先建包");
      const payload = await postJSON(
        "/api/packages/" + encodeURIComponent(packageId) + "/publish", {});
      reloadAfter("已发布 v" + payload.version_no + " · 批次 " + payload.batch_id);
    }));
  }

  document.querySelectorAll(".retire-source-button").forEach((button) => {
    button.addEventListener("click", () => withBusy("作废中", async () => {
      const packageId = button.dataset.package;
      const sourceId = button.dataset.source;
      await postJSON("/api/packages/" + encodeURIComponent(packageId)
        + "/sources/" + encodeURIComponent(sourceId) + "/retire", {});
      reloadAfter("来源 " + sourceId + " 已作废，下次发布不再参与");
    }));
  });
}

bind();
