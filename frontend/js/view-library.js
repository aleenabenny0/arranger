// Your pieces: upload, search, page through, rename, delete.

import { api, newIdempotencyKey, watchJob } from "./api.js";
import { h, replace, field, toast, announce, formatDate, confirmDialog, dialog } from "./dom.js";

const PAGE_SIZE = 12;
const KIND_LABEL = { midi: "MIDI", musicxml: "MusicXML", audio: "Recording", json: "Score data" };

export async function libraryView(root, state) {
  const controller = new AbortController();
  const limits = (state.catalog && state.catalog.limits) || { max_upload_bytes: 40 * 1024 * 1024 };
  const audioReady = Boolean(state.catalog && state.catalog.capabilities.import_audio.available);
  let query = "";
  let offset = 0;
  let uploading = false;

  // --- upload ----------------------------------------------------------------
  const input = h("input", { type: "file", id: "upload-input", class: "visually-hidden", "data-testid": "upload-input",
    accept: `.mid,.midi,.musicxml,.xml,.mxl${audioReady ? ",.wav,.flac,.ogg,.mp3" : ""}` });
  const progress = h("progress", { max: "1", value: "0", "aria-label": "Upload progress" });
  const progressText = h("p", { class: "muted", role: "status" });
  const progressBox = h("div", { class: "progress-box" }, progress, progressText);
  progressBox.hidden = true;
  const uploadError = h("p", { class: "form-error", role: "alert", "data-testid": "upload-error" }); uploadError.hidden = true;
  const dropLabel = h("label", { for: "upload-input", class: "dropzone" },
    h("strong", { text: "Choose a file" }), h("span", { text: " or drop one here" }),
    h("span", { class: "muted block", text: `MIDI or MusicXML${audioReady ? ", or a recording" : ""}. Up to ${Math.round(limits.max_upload_bytes / 1024 / 1024)} MB.` }));

  async function upload(file) {
    if (uploading || !file) return;
    uploadError.hidden = true;
    if (file.size === 0) return fail("That file is empty.");
    if (file.size > limits.max_upload_bytes) return fail(`That file is larger than ${Math.round(limits.max_upload_bytes / 1024 / 1024)} MB.`);
    uploading = true; input.disabled = true; dropLabel.classList.add("is-busy");
    progressBox.hidden = false; progress.removeAttribute("value");
    progressText.textContent = `Uploading ${file.name}…`;
    try {
      const result = await api.upload("/projects/import", file, { filename: file.name },
        { signal: controller.signal, idempotencyKey: newIdempotencyKey() });
      if (result.job) {
        progressText.textContent = "Transcribing the recording. This can take a minute.";
        const job = await watchJob(result.job.id, { signal: controller.signal, onUpdate: (j) => {
          progress.value = j.progress; progressText.textContent = `${j.stage} (${Math.round(j.progress * 100)}%)`;
        } });
        if (job.status !== "succeeded") throw new Error(job.error ? job.error.message : "Transcription was cancelled.");
        toast("Recording transcribed. Check the notes before arranging.", "success");
        window.location.hash = `/project/${job.result.project_id}`;
      } else {
        toast(`Imported “${result.project.title}”.`, "success");
        window.location.hash = `/project/${result.project.id}`;
      }
    } catch (error) {
      if (!error.aborted) fail(error.message);
    } finally {
      uploading = false; input.disabled = false; input.value = ""; dropLabel.classList.remove("is-busy"); progressBox.hidden = true;
    }
  }
  function fail(message) { uploadError.textContent = message; uploadError.hidden = false; announce(message, { urgent: true }); }

  input.addEventListener("change", () => upload(input.files[0]));
  for (const type of ["dragenter", "dragover"]) dropLabel.addEventListener(type, (e) => { e.preventDefault(); dropLabel.classList.add("is-over"); });
  for (const type of ["dragleave", "drop"]) dropLabel.addEventListener(type, (e) => { e.preventDefault(); dropLabel.classList.remove("is-over"); });
  dropLabel.addEventListener("drop", (e) => upload(e.dataTransfer && e.dataTransfer.files[0]));

  // --- list --------------------------------------------------------------------
  const list = h("ul", { class: "project-list", "aria-label": "Your pieces", "data-testid": "project-list" });
  const summary = h("p", { class: "muted", role: "status", "data-testid": "library-summary" });
  const prev = h("button", { type: "button", class: "button", text: "Previous", onclick: () => { offset = Math.max(0, offset - PAGE_SIZE); load(); } });
  const next = h("button", { type: "button", class: "button", text: "Next", onclick: () => { offset += PAGE_SIZE; load(); } });
  const search = h("input", { type: "search", autocomplete: "off", maxlength: 100 });
  let searchTimer = null;
  search.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => { query = search.value.trim(); offset = 0; load(); }, 300);
  });

  async function load() {
    let data;
    try { data = await api.get("/projects", { query: { q: query, limit: PAGE_SIZE, offset }, signal: controller.signal }); }
    catch (error) { if (!error.aborted) summary.textContent = error.message; return; }
    if (!data.projects.length && offset > 0) { offset = Math.max(0, offset - PAGE_SIZE); return load(); }
    replace(list, data.projects.map(row));
    const from = data.total ? offset + 1 : 0;
    summary.textContent = data.total
      ? `Showing ${from} to ${offset + data.projects.length} of ${data.total}${query ? ` matching “${query}”` : ""}.`
      : query ? `Nothing matches “${query}”.` : "No pieces yet. Upload one to begin.";
    prev.disabled = offset === 0; next.disabled = offset + PAGE_SIZE >= data.total;
    return undefined;
  }

  function row(project) {
    const title = h("a", { href: `#/project/${project.id}`, class: "project-title", text: project.title, "data-testid": "project-link" });
    const meta = [KIND_LABEL[project.source_kind] || project.source_kind, project.composer,
      project.bar_count ? `${project.bar_count} bars` : "", `${project.arrangement_count || 0} arrangement${project.arrangement_count === 1 ? "" : "s"}`,
      `changed ${formatDate(project.updated_at)}`].filter(Boolean).join(" · ");
    return h("li", { class: "project-row" }, h("div", {}, title, h("p", { class: "muted", text: meta })),
      h("div", { class: "row-actions" },
        h("button", { type: "button", class: "button", text: "Rename", "aria-label": `Rename ${project.title}`, onclick: () => rename(project) }),
        h("button", { type: "button", class: "button button-danger-quiet", text: "Delete", "aria-label": `Delete ${project.title}`, onclick: () => remove(project) })));
  }

  function rename(project) {
    const name = h("input", { type: "text", value: project.title, maxlength: 200, required: true });
    const nameField = field({ label: "Title", control: name });
    const composer = h("input", { type: "text", value: project.composer || "", maxlength: 200 });
    const save = h("button", { type: "submit", class: "button button-primary", text: "Save" });
    const form = h("form", { method: "dialog", class: "form", onsubmit: async (event) => {
      event.preventDefault();
      if (!name.value.trim()) { nameField.setError("A piece needs a title."); return; }
      save.disabled = true;
      try { await api.patch(`/projects/${project.id}`, { title: name.value.trim(), composer: composer.value.trim() }); el.close(); toast("Saved.", "success"); load(); }
      catch (error) { nameField.setError(error.message); } finally { save.disabled = false; }
    } }, nameField, field({ label: "Composer (optional)", control: composer }));
    const el = dialog({ title: "Rename piece", body: form,
      actions: [h("button", { type: "button", class: "button", text: "Cancel", onclick: () => el.close() }), save] });
    save.addEventListener("click", () => form.requestSubmit());
    name.focus(); name.select();
  }

  async function remove(project) {
    const ok = await confirmDialog({ title: "Delete this piece?", danger: true, confirmLabel: "Delete",
      message: `“${project.title}” and all of its arrangements and files will be deleted. This cannot be undone.` });
    if (!ok) return;
    try { await api.del(`/projects/${project.id}`); toast(`Deleted “${project.title}”.`, "success"); load(); }
    catch (error) { toast(error.message, "error"); }
  }

  replace(root,
    h("h1", { text: "Your pieces" }),
    state.user && !state.user.email_verified ? h("p", { class: "notice", role: "note" },
      "Verify your email address to upload and arrange. ", h("a", { href: "#/account", text: "Send the link again" })) : null,
    h("section", { "aria-labelledby": "upload-heading", class: "panel" },
      h("h2", { id: "upload-heading", text: "Add a piece" }), input, dropLabel, progressBox, uploadError,
      h("p", { class: "muted" }, "Only upload music you have the right to use. ", h("a", { href: "copyright.html", text: "Upload rights" }))),
    h("section", { "aria-labelledby": "list-heading", class: "panel" },
      h("h2", { id: "list-heading", text: "Library" }), field({ label: "Search by title or composer", control: search }), summary, list,
      h("div", { class: "toolbar", role: "group", "aria-label": "Pages" }, prev, next)));
  await load();
  return () => { controller.abort(); window.clearTimeout(searchTimer); };
}
