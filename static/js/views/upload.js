/* 视图 1：图像上传与管理。 */
window.Views = window.Views || {};
window.Views.upload = (function () {
  const C = window.Common;
  let selectedId = null;
  let rootEl = null;

  async function load(el) {
    const grid = el.querySelector("#up-grid");
    const images = await C.fetchImages();
    grid.innerHTML = C.galleryHTML(images);
    C.bindGallery(grid, images, (id, rec) => {
      selectedId = id;
      renderDetail(el, rec);
    });
    if (selectedId) {
      const rec = images.find((i) => i.id === selectedId);
      if (rec) renderDetail(el, rec);
      else renderDetail(el, null);
    }
  }

  function selectRecord(id) {
    selectedId = id;
    C.fetchImages().then((images) => {
      const rec = images.find((i) => i.id === id);
      if (rec && rootEl) renderDetail(rootEl, rec);
    });
  }

  function renderDetail(el, rec) {
    const box = el.querySelector("#up-detail");
    if (!rec) {
      box.innerHTML = `<div class="panel"><div class="panel-title">图像详情</div>
        <div class="empty">选择左侧图像查看元数据</div></div>`;
      return;
    }
    const tags = (rec.tags || []).map((t) => `<span class="tag">${C.esc(t)}</span>`).join(" ") || "<span class='dim'>无</span>";
    const anns = (rec.annotations || []).length;
    box.innerHTML = `
      <div class="panel">
        <div class="panel-title">图像详情 ${rec.duplicate_count > 1 ? `<span class="dup-pill" title="相同内容被上传了多次">🔁 同内容 ${rec.duplicate_count} 条记录</span>` : ""}</div>
        <img src="${rec.file_url}" style="width:100%;border-radius:8px;margin-bottom:10px">
        <div class="keypoint-stats" style="line-height:1.9">
          <div><span class="dim">文件名</span> <strong>${C.esc(rec.filename)}</strong></div>
          <div><span class="dim">尺寸</span> ${rec.width} × ${rec.height}</div>
          <div><span class="dim">格式</span> ${C.esc(rec.format)} · <span class="dim">大小</span> ${C.fmtBytes(rec.size_bytes)}</div>
          <div><span class="dim">内容哈希</span> <span class="mono">${C.esc((rec.content_hash || rec.id || "").slice(0, 16))}…</span></div>
          <div><span class="dim">上传时间</span> ${C.fmtDate(rec.created_at)}</div>
          <div><span class="dim">标签</span> ${tags}</div>
          <div><span class="dim">标注</span> ${anns} 条</div>
          ${rec.note ? `<div><span class="dim">备注</span> ${C.esc(rec.note)}</div>` : ""}
        </div>
        <div id="up-peers"></div>
        <div class="toolbar" style="margin-top:12px">
          <button class="btn btn-sm" id="up-rename">重命名</button>
          <button class="btn btn-sm" id="up-tags">编辑标签</button>
          <button class="btn btn-sm" id="up-note">备注</button>
          <button class="btn btn-sm btn-danger" id="up-del">删除</button>
        </div>
      </div>`;

    // 同内容的其它上传记录
    C.fetchImages().then((images) => {
      const peers = images.filter((i) => i.content_hash === rec.content_hash && i.id !== rec.id);
      const peersBox = box.querySelector("#up-peers");
      if (!peersBox) return;
      if (!peers.length) {
        peersBox.innerHTML = "";
        return;
      }
      peersBox.innerHTML = `<div style="margin-top:10px"><span class="dim">相同内容的其它记录（${peers.length}）：</span>
        <div class="peer-list">${peers.map((p) => `
          <span class="peer-chip" data-id="${C.esc(p.id)}" title="${C.esc(p.filename)}">
            📎 ${C.esc(p.filename)} <span class="dim">${C.fmtDate(p.created_at)}</span>
          </span>`).join("")}</div></div>`;
      peersBox.querySelectorAll(".peer-chip").forEach((chip) => {
        chip.onclick = () => selectRecord(chip.dataset.id);
      });
    });

    box.querySelector("#up-del").onclick = async () => {
      const multi = rec.duplicate_count > 1;
      const msg = multi
        ? "确认删除这条记录？\n同内容的其它记录与物理文件不受影响（历史结果仍保留）。"
        : "确认删除该图像？（历史结果仍保留）";
      if (!confirm(msg)) return;
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
   * 上传：默认先查重（on_duplicate=ask）。后端发现内容重复时不写入任何记录，
   * 返回 duplicates 让用户逐张选择；确认后带逐文件策略重发。
   * ------------------------------------------------------------------ */
  async function doUpload(files) {
    if (!files || !files.length) return;
    files = Array.from(files);
    try {
      const r = await Api.upload(files, { on_duplicate: "ask" });
      if (r.need_choice) {
        showDuplicateDialog(files, r.duplicates, r.pending || []);
        return;
      }
      reportUpload(r);
    } catch (e) {
      C.toast("上传失败：" + e.message, "error");
    }
  }

  function reportUpload(r) {
    if (r.saved && r.saved.length) C.toast(`已保存 ${r.saved.length} 条新记录`, "success");
    if (r.merged && r.merged.length) {
      const renamed = r.merged.filter((m) => m.status === "merged_new").length;
      C.toast(renamed
        ? `${renamed} 条已有记录已改用新名字/标签，其余 ${r.merged.length - renamed} 条合并未改名`
        : `${r.merged.length} 张与已有内容相同，已合并到原记录`, "info");
    }
    if (r.skipped && r.skipped.length) {
      C.toast(`${r.skipped.length} 张被跳过（过大或格式问题）`, "error");
    }
    C.refreshImages().then(() => rootEl && load(rootEl));
  }

  function showDuplicateDialog(files, duplicates, pending) {
    const urls = [];
    const thumbUrl = (idx) => {
      const u = URL.createObjectURL(files[idx]);
      urls.push(u);
      return u;
    };

    const row = (d) => {
      const ex = d.existing;
      const peerNote = (ex === null && d.batch_peer_index != null)
        ? `<div class="dim" style="margin-top:4px">与本次上传的第 ${d.batch_peer_index + 1} 张内容相同</div>` : "";
      const existingBlock = ex ? `
        <div class="dup-existing">
          <span class="dim">已有记录：</span>
          <strong>${C.esc(ex.filename)}</strong>
          <span class="dim">· ${C.fmtDate(ex.created_at)} 上传${(ex.tags || []).length ? " · 标签 " + ex.tags.map(C.esc).join(", ") : ""}</span>
        </div>` : peerNote;
      return `
      <div class="dup-row" data-index="${d.index}">
        <img class="dup-thumb" src="${thumbUrl(d.index)}">
        <div class="dup-body">
          <div class="dup-name">${C.esc(d.filename)}</div>
          ${existingBlock}
          <div class="field" style="margin-top:6px">
            <label>本次上传使用的名字</label>
            <input type="text" class="dup-filename" value="${C.esc(d.filename)}">
          </div>
          <div class="field" style="margin-top:6px">
            <label>标签（逗号分隔）</label>
            <input type="text" class="dup-tags" placeholder="例如：封面, 印刷用">
          </div>
          <div class="dup-opts" style="margin-top:6px">
            <label><input type="radio" name="dup-${d.index}" value="keep" checked>
              <strong>保留独立记录</strong><span class="dim">（新名字、新标签，与旧记录并存）</span></label>
            <label><input type="radio" name="dup-${d.index}" value="merge_new">
              合并并<strong>改用新名字/标签</strong></label>
            <label><input type="radio" name="dup-${d.index}" value="merge">
              合并，<strong>保留已有名字</strong>（忽略本次名字/标签）</label>
          </div>
        </div>
      </div>`;
    };

    const html = `
      <div class="dup-hint">检测到 <strong>${duplicates.length}</strong> 张与已有内容（或本批次其它文件）完全相同的图片。
      相同内容可以用不同名字/标签分别保存，请逐张选择处理方式：</div>
      <div class="field dup-apply">
        <label>批量应用到以上全部：</label>
        <select class="dup-apply-sel">
          <option value="">— 选择操作 —</option>
          <option value="keep">全部保留独立记录</option>
          <option value="merge_new">全部合并并改用新名字/标签</option>
          <option value="merge">全部合并、保留旧名字</option>
        </select>
      </div>
      ${duplicates.map(row).join("")}
      ${pending.length ? `<div class="dim" style="margin:10px 0">另有 ${pending.length} 张内容全新的图片，将一并上传。</div>` : ""}
      <div class="modal-actions">
        <button class="btn" id="dup-cancel">取消（都不上传）</button>
        <button class="btn btn-primary" id="dup-ok">确认上传</button>
      </div>`;
    const m = C.modal(html, `发现 ${duplicates.length} 张内容相同的图片`);

    const cleanup = () => { urls.forEach((u) => URL.revokeObjectURL(u)); };
    const close = () => { cleanup(); m.close(); };

    m.el.querySelector(".dup-apply-sel").onchange = (e) => {
      const v = e.target.value;
      if (!v) return;
      duplicates.forEach((d) => {
        const radio = m.el.querySelector(`input[name="dup-${d.index}"][value="${v}"]`);
        if (radio) radio.checked = true;
      });
    };

    m.el.querySelector("#dup-cancel").onclick = close;
    m.el.querySelector("#dup-ok").onclick = async () => {
      const fields = { on_duplicate: "keep" };   // 全局兜底：无重复的直接建记录
      for (const d of duplicates) {
        const rowEl = m.el.querySelector(`.dup-row[data-index="${d.index}"]`);
        const policy = rowEl.querySelector(`input[name="dup-${d.index}"]:checked`).value;
        fields[`on_duplicate_${d.index}`] = policy;
        fields[`filename_${d.index}`] = rowEl.querySelector(".dup-filename").value || d.filename;
        fields[`tags_${d.index}`] = rowEl.querySelector(".dup-tags").value;
      }
      cleanup();
      m.close();
      try {
        const r = await Api.upload(files, fields);
        reportUpload(r);
      } catch (e) {
        C.toast("上传失败：" + e.message, "error");
      }
    };
  }

  return {
    mount(el) {
      rootEl = el;
      el.innerHTML = `
        <div class="row">
          <div class="col" style="flex:1;min-width:0">
            <div class="panel">
              <div class="panel-title">上传图像<span class="dim">支持 PNG/JPG/BMP/WebP，单张 ≤ 25MB；相同内容可用不同名字/标签分别保存，重复时会提示</span></div>
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
          <div class="col" style="width:320px;flex:none" id="up-detail"></div>
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
    refresh() { C.refreshImages().then(() => rootEl && load(rootEl)); },
  };
})();
