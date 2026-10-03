/* 视图 1：图像上传与管理。 */
window.Views = window.Views || {};
window.Views.upload = (function () {
  const C = window.Common;
  let selectedId = null;

  function rootEl() {
    return document.querySelector('.view[data-view="upload"]');
  }

  async function load(el) {
    const grid = el.querySelector("#up-grid");
    const images = await C.fetchImages();
    grid.innerHTML = C.galleryHTML(images);
    C.bindGallery(grid, images, (id, rec) => {
      selectedId = id;
      renderDetail(el, rec, images);
    });
    if (selectedId) {
      const rec = images.find((i) => i.id === selectedId);
      if (rec) renderDetail(el, rec, images);
      else renderDetail(el, null, images);
    }
  }

  async function renderDetail(el, rec, images) {
    const box = el.querySelector("#up-detail");
    if (!rec) {
      box.innerHTML = `<div class="panel"><div class="panel-title">图像详情</div>
        <div class="empty">选择左侧图像查看元数据</div></div>`;
      return;
    }
    const tags = (rec.tags || []).map((t) => `<span class="tag">${C.esc(t)}</span>`).join(" ") || "<span class='dim'>无</span>";
    const anns = (rec.annotations || []).length;

    // 同内容（同哈希）的其他上传记录
    images = images || await C.fetchImages();
    const twins = images.filter((i) => i.hash && i.hash === rec.hash);
    const twinsHTML = twins.length > 1 ? `
      <div class="dup-box">
        <div class="dup-title">🔁 相同内容已上传 ${twins.length} 次</div>
        ${twins.map((t) => `
          <div class="dup-item ${t.id === rec.id ? "is-self" : ""}" data-id="${C.esc(t.id)}" title="点击查看该记录">
            <img src="${C.esc(t.thumbnail_url)}" alt="">
            <div class="dup-info">
              <div class="dup-name">${C.esc(t.filename)}${t.id === rec.id ? " <span class='dim'>（当前）</span>" : ""}</div>
              <div class="dim">${C.fmtDate(t.created_at)} · ${(t.tags || []).map(C.esc).join(" / ") || "无标签"}</div>
            </div>
          </div>`).join("")}
      </div>` : "";

    box.innerHTML = `
      <div class="panel">
        <div class="panel-title">图像详情${rec.dup_count > 1 ? ` <span class="dup-pill">重复 ${rec.dup_count} 次</span>` : ""}</div>
        <img src="${rec.file_url}" style="width:100%;border-radius:8px;margin-bottom:10px">
        <div class="keypoint-stats" style="line-height:1.9">
          <div><span class="dim">文件名</span> <strong>${C.esc(rec.filename)}</strong></div>
          <div><span class="dim">尺寸</span> ${rec.width} × ${rec.height}</div>
          <div><span class="dim">格式</span> ${C.esc(rec.format)} · <span class="dim">大小</span> ${C.fmtBytes(rec.size_bytes)}</div>
          <div><span class="dim">哈希</span> <span class="mono">${rec.hash.slice(0, 16)}…</span></div>
          <div><span class="dim">上传时间</span> ${C.fmtDate(rec.created_at)}</div>
          <div><span class="dim">标签</span> ${tags}</div>
          <div><span class="dim">标注</span> ${anns} 条</div>
          ${rec.note ? `<div><span class="dim">备注</span> ${C.esc(rec.note)}</div>` : ""}
        </div>
        ${twinsHTML}
        <div class="toolbar" style="margin-top:12px">
          <button class="btn btn-sm" id="up-rename">重命名</button>
          <button class="btn btn-sm" id="up-tags">编辑标签</button>
          <button class="btn btn-sm" id="up-note">备注</button>
          <button class="btn btn-sm btn-danger" id="up-del">删除</button>
        </div>
      </div>`;

    // 点击同内容版本直接跳转查看
    box.querySelectorAll(".dup-item").forEach((item) => {
      item.onclick = () => {
        const target = images.find((i) => i.id === item.dataset.id);
        if (!target) return;
        selectedId = target.id;
        const grid = el.querySelector("#up-grid");
        grid.querySelectorAll(".card.selected").forEach((c) => c.classList.remove("selected"));
        const card = grid.querySelector(`.card[data-id="${target.id}"]`);
        if (card) card.classList.add("selected");
        renderDetail(el, target, images);
      };
    });

    box.querySelector("#up-del").onclick = async () => {
      const tip = twins.length > 1
        ? `确认删除该记录「${rec.filename}」？\n同内容还有 ${twins.length - 1} 条记录，物理图片会保留；这是最后一条时才删除文件。`
        : "确认删除该图像？（历史结果仍保留）";
      if (!confirm(tip)) return;
      await Api.del(`/api/images/${rec.id}`);
      selectedId = null;
      C.toast("已删除", "success");
      await C.refreshImages();
      load(el);
    };
    box.querySelector("#up-rename").onclick = () => editField(el, rec, "filename", "重命名");
    box.querySelector("#up-tags").onclick = () => editField(el, rec, "tags", "标签（逗号分隔）");
    box.querySelector("#up-note").onclick = () => editField(el, rec, "note", "备注");
  }

  function editField(el, rec, field, label) {
    const m = C.modal(`<div class="field"><label>${label}</label>
      <input type="text" id="mf-val" value="${C.esc(field === "tags" ? (rec.tags || []).join(",") : rec[field] || "")}"></div>
      <div class="modal-actions"><button class="btn" id="mf-cancel">取消</button>
      <button class="btn btn-primary" id="mf-ok">保存</button></div>`, label);
    m.el.querySelector("#mf-cancel").onclick = m.close;
    m.el.querySelector("#mf-ok").onclick = async () => {
      let v = m.el.querySelector("#mf-val").value;
      const body = {};
      if (field === "tags") body.tags = v.split(",").map((s) => s.trim()).filter(Boolean);
      else body[field] = v;
      await Api.patch(`/api/images/${rec.id}`, body);
      m.close();
      C.toast("已保存", "success");
      await C.refreshImages();
      load(el);
    };
  }

  /* ------------------------------------------------------------------
   * 上传：先发「询问」请求；若有内容重复，弹窗逐份让用户选择处理方式，
   * 再按选择重发，绝不静默吞掉本次上传的文件名。
   * ------------------------------------------------------------------ */
  async function doUpload(files) {
    if (!files || !files.length) return;
    const el = rootEl();
    const list = Array.from(files);
    try {
      const r = await Api.upload(list, { dupPolicy: "ask" });
      if (r.duplicates && r.duplicates.length) {
        const decision = await askDuplicateDecision(r.duplicates);
        if (decision === null) {
          // 用户取消：非重复的已入库，仅提示；重复项未处理
          if (r.saved.length) C.toast(`已上传 ${r.saved.length} 张，重复项已取消`, "success");
          else C.toast("已取消重复上传", "");
          await C.refreshImages();
          return load(el);
        }
        const policies = {};
        const names = {};
        for (const d of r.duplicates) {
          const choice = decision[d.index];
          if (choice && choice.action !== "skip") {
            policies[d.index] = choice.action;
            if (choice.action === "copy" && choice.name) names[d.index] = choice.name;
          }
        }
        const r2 = await Api.upload(list, { dupPolicy: "ask", policies, names });
        // 首次请求里已直接入库的新图（r.saved）与第二次的结果合并提示
        reportResult({
          saved: (r.saved || []).concat(r2.saved || []),
          skipped: (r2.skipped || []),
        }, r.duplicates.length);
      } else {
        reportResult(r, 0);
      }
      await C.refreshImages();
      load(el);
    } catch (e) {
      C.toast("上传失败：" + e.message, "error");
    }
  }

  function reportResult(r, pendingDupCount) {
    const created = (r.saved || []).filter((s) => s.upload_status === "created").length;
    const copies = (r.saved || []).filter((s) => s.upload_status === "copy").length;
    const overwritten = (r.saved || []).filter((s) => s.upload_status === "overwritten").length;
    const reused = (r.saved || []).filter((s) => s.upload_status === "reused").length;
    const skippedDup = pendingDupCount - (copies + overwritten + reused);
    if (created) C.toast(`新上传 ${created} 张`, "success");
    if (copies) C.toast(`已为 ${copies} 个重复内容建立独立记录（新名字已保留）`, "success");
    if (overwritten) C.toast(`已用新名字覆盖 ${overwritten} 条旧记录`, "success");
    if (reused) C.toast(`${reused} 个文件与已有内容相同，沿用首次上传的记录`, "");
    if (skippedDup > 0) C.toast(`${skippedDup} 个重复文件未处理（保留旧记录）`, "");
    if (r.skipped && r.skipped.length) {
      C.toast(`${r.skipped.length} 张被跳过（过大或格式问题）`, "error");
    }
    if (!r.saved.length && !(r.skipped && r.skipped.length)) C.toast("没有需要上传的文件", "");
  }

  /* 返回 Promise<{index: {action:"copy"|"overwrite"|"reuse"|"skip", name?}} | null>。 */
  function askDuplicateDecision(duplicates) {
    return new Promise((resolve) => {
      const rows = duplicates.map((d, row) => {
        const ex = d.existing[0];
        const others = d.existing.length;
        return `
        <div class="dup-row" data-index="${d.index}">
          <div class="dup-row-head">
            <img src="${C.esc(ex.thumbnail_url)}" alt="">
            <div>
              <div><strong>${C.esc(d.filename)}</strong></div>
              <div class="dim">与已有图像内容完全相同（${others > 1 ? `已上传 ${others} 次，` : ""}最早：${C.esc(ex.filename)} · ${C.fmtDate(ex.created_at)}）</div>
            </div>
          </div>
          <div class="dup-choices">
            <label><input type="radio" name="dup-${row}" value="copy" checked>
              建新记录，保留两个名字</label>
            <label><input type="radio" name="dup-${row}" value="overwrite">
              用新名字覆盖旧记录</label>
            <label><input type="radio" name="dup-${row}" value="reuse">
              沿用旧名字（不新增）</label>
            <label><input type="radio" name="dup-${row}" value="skip">
              本次跳过该文件</label>
          </div>
          <div class="dup-newname">
            <label>新记录的文件名：
              <input type="text" class="dup-name-input" value="${C.esc(d.filename)}"></label>
          </div>
        </div>`;
      }).join("");

      const m = C.modal(`
        <div class="dup-warn">⚠️ 检测到 ${duplicates.length} 个文件的内容与已有图像完全相同。
          系统不会再静默合并，请逐份选择处理方式：</div>
        ${rows}
        <div class="modal-actions">
          <button class="btn" id="dup-cancel">全部取消</button>
          <button class="btn-primary btn" id="dup-ok">按选择处理</button>
        </div>`, "发现重复上传");

      m.el.querySelector("#dup-cancel").onclick = () => { m.close(); resolve(null); };
      m.el.querySelector("#dup-ok").onclick = () => {
        const out = {};
        let bad = false;
        m.el.querySelectorAll(".dup-row").forEach((rowEl, row) => {
          const index = Number(rowEl.dataset.index);
          const action = m.el.querySelector(`input[name="dup-${row}"]:checked`).value;
          const name = rowEl.querySelector(".dup-name-input").value.trim();
          if (action === "copy" && !name) { bad = true; }
          out[index] = { action, name };
        });
        if (bad) { C.toast("建新记录时文件名不能为空", "error"); return; }
        m.close();
        resolve(out);
      };

      // 仅「建新记录」时可编辑新文件名；选中其他选项时收起
      m.el.querySelectorAll(".dup-row").forEach((rowEl, row) => {
        const nameBox = rowEl.querySelector(".dup-newname");
        rowEl.querySelectorAll(`input[name="dup-${row}"]`).forEach((radio) => {
          radio.addEventListener("change", () => {
            nameBox.style.display = radio.value === "copy" ? "" : "none";
          });
        });
      });
    });
  }

  return {
    mount(el) {
      el.innerHTML = `
        <div class="row">
          <div class="col" style="flex:1;min-width:0">
            <div class="panel">
              <div class="panel-title">上传图像<span class="dim">支持 PNG/JPG/BMP/WebP，单张 ≤ 25MB；内容相同的重复上传会先提示你选择，不再静默合并</span></div>
              <div id="up-drop" style="border:2px dashed var(--border-strong);border-radius:10px;padding:26px;text-align:center;color:var(--text-dim);cursor:pointer;transition:border-color .12s">
                <div style="font-size:26px">📥</div>
                <div>拖拽图片到此处，或点击选择文件</div>
              </div>
              <div class="toolbar" style="margin-top:12px;margin-bottom:0">
                <button class="btn btn-primary" id="up-btn">选择文件上传</button>
                <input type="file" id="up-file" multiple accept="image/*" hidden>
                <button class="btn" id="up-refresh">刷新</button>
              </div>
            </div>
            <div id="up-grid"></div>
          </div>
          <div class="col" style="width:340px;flex:none" id="up-detail"></div>
        </div>`;

      const drop = el.querySelector("#up-drop");
      const fileInput = el.querySelector("#up-file");
      el.querySelector("#up-btn").onclick = () => fileInput.click();
      fileInput.onchange = () => { doUpload(fileInput.files); fileInput.value = ""; };
      drop.onclick = () => fileInput.click();
      drop.ondragover = (e) => { e.preventDefault(); drop.style.borderColor = "var(--accent)"; };
      drop.ondragleave = () => { drop.style.borderColor = "var(--border-strong)"; };
      drop.ondrop = (e) => { e.preventDefault(); drop.style.borderColor = "var(--border-strong)"; doUpload(e.dataTransfer.files); };
      el.querySelector("#up-refresh").onclick = () => { C.invalidate("images"); load(el); };

      renderDetail(el, null);
      load(el);
    },
    refresh() { C.refreshImages().then(() => this.mounted && load(rootEl())); },
  };
})();
