// Your pieces: upload, search, page through, rename, delete.
import { useCallback, useEffect, useRef, useState, type DragEvent, type FormEvent } from "react";
import { ApiError, client, uploadFile, watchJob } from "../api/client";
import type { ProjectList, ProjectRow } from "../api/types";
import { ConfirmDialog, Dialog } from "../components/Dialog";
import { Field } from "../components/Field";
import { setTitle, useFocusHeading } from "../lib/focus";
import { formatDate } from "../lib/format";
import { newIdempotencyKey } from "../lib/format";
import { go } from "../lib/route";
import { useSession } from "../lib/session";
import { useToast } from "../lib/toast";

export const PAGE_SIZE = 12;
const KIND_LABEL: Record<string, string> = { midi: "MIDI", musicxml: "MusicXML", audio: "Recording", json: "Score data" };

export function summaryText(data: ProjectList, offset: number, query: string): string {
  if (data.total) {
    const from = offset + 1;
    return `Showing ${from} to ${offset + data.projects.length} of ${data.total}${query ? ` matching “${query}”` : ""}.`;
  }
  return query ? `Nothing matches “${query}”.` : "No pieces yet. Upload one to begin.";
}

export function rowMeta(project: ProjectRow): string {
  return [
    KIND_LABEL[project.source_kind] || project.source_kind, project.composer,
    project.bar_count ? `${project.bar_count} bars` : "",
    `${project.arrangement_count || 0} arrangement${project.arrangement_count === 1 ? "" : "s"}`,
    `changed ${formatDate(project.updated_at)}`,
  ].filter(Boolean).join(" · ");
}

interface RenameProps { project: ProjectRow; onClose: () => void; onSaved: () => void }

function RenameDialog({ project, onClose, onSaved }: RenameProps) {
  const { toast } = useToast();
  const [title, setName] = useState(project.title);
  const [composer, setComposer] = useState(project.composer || "");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event?: FormEvent) {
    if (event) event.preventDefault();
    if (!title.trim()) { setError("A piece needs a title."); return; }
    setBusy(true);
    try {
      await client.PATCH("/projects/{project_id}", { params: { path: { project_id: project.id } }, body: { title: title.trim(), composer: composer.trim() } });
      toast("Saved.", "success");
      onSaved();
      onClose();
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog title="Rename piece" onClose={onClose} actions={
      <>
        <button type="button" className="button" onClick={onClose}>Cancel</button>
        <button type="button" className="button button-primary" disabled={busy} onClick={() => void submit()}>Save</button>
      </>
    }>
      <form method="dialog" className="form" onSubmit={submit}>
        <Field label="Title" error={error}>
          <input type="text" value={title} maxLength={200} required autoFocus onFocus={(e) => e.target.select()} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Composer (optional)">
          <input type="text" value={composer} maxLength={200} onChange={(e) => setComposer(e.target.value)} />
        </Field>
      </form>
    </Dialog>
  );
}

export function Library() {
  const { user, catalog } = useSession();
  const { toast, announce } = useToast();
  const limits = (catalog && catalog.limits) || { max_upload_bytes: 40 * 1024 * 1024 };
  const audioReady = Boolean(catalog && catalog.capabilities.import_audio && catalog.capabilities.import_audio.available);
  const maxMb = Math.round(limits.max_upload_bytes / 1024 / 1024);

  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<ProjectList | null>(null);
  const [summary, setSummary] = useState("");
  const [uploading, setUploading] = useState(false);
  const [over, setOver] = useState(false);
  const [progress, setProgress] = useState<{ value: number | null; text: string } | null>(null);
  const [uploadError, setUploadError] = useState("");
  const [renaming, setRenaming] = useState<ProjectRow | null>(null);
  const [deleting, setDeleting] = useState<ProjectRow | null>(null);
  const controller = useRef(new AbortController());
  const input = useRef<HTMLInputElement>(null);
  setTitle("Your pieces");
  useFocusHeading(true, "library");

  useEffect(() => {
    const current = new AbortController();
    controller.current = current;
    return () => current.abort();
  }, []);

  const load = useCallback(async (at: number, q: string) => {
    let result: ProjectList;
    try {
      const { data: body } = await client.GET("/projects", { params: { query: { q, limit: PAGE_SIZE, offset: at } }, signal: controller.current.signal });
      result = body as unknown as ProjectList;
    } catch (error) {
      if (!(error instanceof ApiError && error.aborted)) setSummary((error as Error).message);
      return;
    }
    if (!result.projects.length && at > 0) { setOffset(Math.max(0, at - PAGE_SIZE)); return; }
    setData(result);
    setSummary(summaryText(result, at, q));
  }, []);

  useEffect(() => { void load(offset, query); }, [load, offset, query]);

  useEffect(() => {
    const timer = window.setTimeout(() => { setQuery(search.trim()); setOffset(0); }, 300);
    return () => window.clearTimeout(timer);
  }, [search]);

  function fail(message: string) {
    setUploadError(message);
    announce(message, { urgent: true });
  }

  async function upload(file: File | null | undefined) {
    if (uploading || !file) return;
    setUploadError("");
    if (file.size === 0) { fail("That file is empty."); return; }
    if (file.size > limits.max_upload_bytes) { fail(`That file is larger than ${maxMb} MB.`); return; }
    setUploading(true);
    setProgress({ value: null, text: `Uploading ${file.name}…` });
    try {
      const result = await uploadFile(file, { signal: controller.current.signal, idempotencyKey: newIdempotencyKey() }) as
        { job?: { id: string }; project?: { id: string; title: string } };
      if (result.job) {
        setProgress({ value: null, text: "Transcribing the recording. This can take a minute." });
        const job = await watchJob(result.job.id, { signal: controller.current.signal, onUpdate: (j) => {
          setProgress({ value: j.progress, text: `${j.stage} (${Math.round(j.progress * 100)}%)` });
        } });
        if (job.status !== "succeeded") throw new Error(job.error ? job.error.message : "Transcription was cancelled.");
        toast("Recording transcribed. Check the notes before arranging.", "success");
        go(`/project/${(job.result as { project_id: string }).project_id}`);
      } else if (result.project) {
        toast(`Imported “${result.project.title}”.`, "success");
        go(`/project/${result.project.id}`);
      }
    } catch (error) {
      if (!(error instanceof ApiError && error.aborted)) fail((error as Error).message);
    } finally {
      setUploading(false);
      setProgress(null);
      if (input.current) input.current.value = "";
    }
  }

  function onDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setOver(false);
    void upload(event.dataTransfer && event.dataTransfer.files[0]);
  }

  async function remove(project: ProjectRow) {
    try {
      await client.DELETE("/projects/{project_id}", { params: { path: { project_id: project.id } } });
      toast(`Deleted “${project.title}”.`, "success");
      void load(offset, query);
    } catch (error) {
      toast((error as Error).message, "error");
    }
  }

  const total = data ? data.total : 0;

  return (
    <>
      <h1>Your pieces</h1>
      {user && !user.email_verified ? (
        <p className="notice" role="note">Verify your email address to upload and arrange. <a href="#/account">Send the link again</a></p>
      ) : null}
      <section aria-labelledby="upload-heading" className="panel">
        <h2 id="upload-heading">Add a piece</h2>
        <input
          ref={input}
          type="file"
          id="upload-input"
          className="visually-hidden"
          data-testid="upload-input"
          accept={`.mid,.midi,.musicxml,.xml,.mxl,.json${audioReady ? ",.wav,.flac,.ogg,.mp3" : ""}`}
          disabled={uploading}
          onChange={(event) => void upload(event.target.files && event.target.files[0])}
        />
        <label
          htmlFor="upload-input"
          className={`dropzone${over ? " is-over" : ""}${uploading ? " is-busy" : ""}`}
          onDragEnter={(e) => { e.preventDefault(); setOver(true); }}
          onDragOver={(e) => { e.preventDefault(); setOver(true); }}
          onDragLeave={(e) => { e.preventDefault(); setOver(false); }}
          onDrop={onDrop}
        >
          <strong>Choose a file</strong><span> or drop one here</span>
          <span className="muted block">{`MIDI or MusicXML${audioReady ? ", or a recording" : ""}. Up to ${maxMb} MB.`}</span>
        </label>
        <div className="progress-box" hidden={!progress}>
          <progress max="1" value={progress && progress.value !== null ? progress.value : undefined} aria-label="Upload progress" />
          <p className="muted" role="status">{progress ? progress.text : ""}</p>
        </div>
        <p className="form-error" role="alert" data-testid="upload-error" hidden={!uploadError}>{uploadError}</p>
        <p className="muted">Only upload music you have the right to use. <a href="copyright.html">Upload rights</a></p>
      </section>
      <section aria-labelledby="list-heading" className="panel">
        <h2 id="list-heading">Library</h2>
        <Field label="Search by title or composer">
          <input type="search" autoComplete="off" maxLength={100} value={search} onChange={(e) => setSearch(e.target.value)} />
        </Field>
        <p className="muted" role="status" data-testid="library-summary">{summary}</p>
        <ul className="project-list" aria-label="Your pieces" data-testid="project-list">
          {(data ? data.projects : []).map((project) => (
            <li key={project.id} className="project-row">
              <div>
                <a href={`#/project/${project.id}`} className="project-title" data-testid="project-link">{project.title}</a>
                <p className="muted">{rowMeta(project)}</p>
              </div>
              <div className="row-actions">
                <button type="button" className="button" aria-label={`Rename ${project.title}`} onClick={() => setRenaming(project)}>Rename</button>
                <button type="button" className="button button-danger-quiet" aria-label={`Delete ${project.title}`} onClick={() => setDeleting(project)}>Delete</button>
              </div>
            </li>
          ))}
        </ul>
        <div className="toolbar" role="group" aria-label="Pages">
          <button type="button" className="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</button>
          <button type="button" className="button" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>Next</button>
        </div>
      </section>
      {renaming ? <RenameDialog project={renaming} onClose={() => setRenaming(null)} onSaved={() => void load(offset, query)} /> : null}
      {deleting ? (
        <ConfirmDialog
          title="Delete this piece?"
          danger
          confirmLabel="Delete"
          message={`“${deleting.title}” and all of its arrangements and files will be deleted. This cannot be undone.`}
          onResult={(ok) => { const target = deleting; setDeleting(null); if (ok) void remove(target); }}
        />
      ) : null}
    </>
  );
}
