/*
 * Nuage Libre: file manager of the private cloud (left panel).
 *
 * Responsibilities:
 *   - browse the cloud folder by folder: sub-folders (GET /api/folders),
 *     files (GET /api/files?folder_id=...), path of the current folder for
 *     the breadcrumb (GET /api/folders/{id}), totals (GET /api/files/summary);
 *   - create folders (POST /api/folders);
 *   - upload files AND whole folders, by drag & drop or with the pickers
 *     (POST /api/files?folder_id=...);
 *   - offer "View" / "Download" links (GET /api/files/{id}/download).
 *
 * Upload protocol (see app/routers/files.py for the reasons): the body of
 * the request IS the file, not a multipart form.
 *
 *     POST /api/files?folder_id=<destination, absent = root>
 *     Content-Type: application/octet-stream
 *     X-File-Name: <encodeURIComponent(file.name)>
 *     <raw bytes>
 *
 * XMLHttpRequest is used instead of fetch() for one reason: fetch() cannot
 * report UPLOAD progress, and a progress bar matters for a 2 GB video.
 * Uploads are sent one at a time (a queue): they do not compete for the
 * bandwidth and the disk, and each one shows a meaningful progress.
 *
 * Folders. Folders only exist in the database (the bytes of every file stay
 * at "<user>/<file id>" on the server's disk). A folder dropped from the
 * computer is recreated in the cloud, sub-folders included, then its files
 * are sent one by one into the right folders; it shows as ONE row of the
 * queue with a global progress. If a folder with the same name already
 * exists, the server picks a free name ("Photos (2)", on_conflict =
 * "rename"): an upload never merges into existing content.
 *
 * Navigation. The current folder is kept in the address (#folder=<id>), so
 * the browser's Back button goes back up and a reload stays in the folder.
 *
 * Security: file and folder names come from the user, and once stored they
 * come back from the server; they are only ever inserted with `textContent`,
 * never interpreted as HTML (see the security rules at the top of js/app.js).
 *
 * Other scripts are told when the totals change through a "cloud:changed"
 * event on document (js/app.js uses it for the RAG status line).
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const panel = $('files-panel');
  const panelInner = panel.querySelector('.files');
  const listEl = $('file-list');
  const queueEl = $('upload-queue');
  const emptyEl = $('files-empty');
  const emptyTitleEl = $('files-empty-title');
  const emptyTextEl = $('files-empty-text');
  const errorEl = $('files-error');
  const noticeEl = $('files-notice');
  const summaryEl = $('files-summary');
  const fileInput = $('file-input');
  const folderInput = $('folder-input');
  const pathEl = $('folder-path');
  const breadcrumbEl = $('breadcrumb');
  const folderForm = $('folder-form');
  const folderNameInput = $('folder-name');
  const folderFormError = $('folder-form-error');
  const folderCreateBtn = folderForm.querySelector('.folder-form-btn--create');
  const newFolderBtn = $('new-folder-btn');
  const uploadFolderBtn = $('upload-folder-btn');

  /** Return a fresh copy of a <template>'s content (icon, mascot...). */
  const cloneTemplate = (id) => $(id).content.firstElementChild.cloneNode(true);

  // Dates and numbers follow the language of the page (<html lang="en">).
  const locale = document.documentElement.lang || undefined;

  // A folder upload larger than this is refused before anything is sent
  // (reading a huge tree such as node_modules would freeze the tab). The
  // server has its own limits: file size, free disk space, folder depth.
  const MAX_FOLDER_UPLOAD_FILES = 10000;
  // Files that operating systems scatter in folders: not the user's data.
  const IGNORED_FILES = new Set(['.DS_Store', 'Thumbs.db', 'desktop.ini']);
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

  // ---------------------------------------------------------------- state
  let currentFolderId = null;    // folder shown; null = the root of the cloud
  let currentPath = [];          // [{id, name}] from the root to it (empty at the root)
  let folders = [];              // FolderOut objects of the current folder, by name
  let files = [];                // FileOut objects of the current folder, newest first
  const renderedIds = new Set(); // ids already on screen: only NEW rows animate
  let firstRender = true;
  let loadToken = 0;             // ignores the answer of an outdated navigation
  const pending = [];            // upload jobs waiting for their turn
  let uploading = false;

  // ------------------------------------------------------------ formatting
  /** 1534000 -> "1.5 MB". Decimal units, as shown by macOS and phones. */
  function formatBytes(bytes) {
    const units = ['B', 'kB', 'MB', 'GB', 'TB'];
    let value = bytes;
    let unit = 0;
    while (value >= 1000 && unit < units.length - 1) {
      value /= 1000;
      unit += 1;
    }
    const number = new Intl.NumberFormat(locale, { maximumFractionDigits: unit === 0 ? 0 : 1 }).format(value);
    return `${number} ${units[unit]}`;
  }

  const dateFormat = new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' });
  const formatDate = (iso) => dateFormat.format(new Date(iso));
  const plural = (count, word) => `${new Intl.NumberFormat(locale).format(count)} ${word}${count === 1 ? '' : 's'}`;

  /** "Rapport.final.PDF" -> "PDF" (at most 4 letters, for the badge). */
  function badgeLabel(name) {
    const dot = name.lastIndexOf('.');
    if (dot <= 0 || dot === name.length - 1) return 'FILE';
    return name.slice(dot + 1).toUpperCase().slice(0, 4);
  }

  const ARCHIVE_TYPES = new Set([
    'application/zip', 'application/gzip', 'application/x-tar', 'application/x-7z-compressed',
    'application/vnd.rar', 'application/x-rar-compressed', 'application/x-bzip2', 'application/x-xz',
  ]);

  /** Family of a MIME type, used only to tint the badge (css/files.css). */
  function fileKind(mimeType) {
    const type = mimeType || '';
    if (type.startsWith('image/')) return 'image';
    if (type.startsWith('audio/') || type.startsWith('video/')) return 'media';
    if (ARCHIVE_TYPES.has(type)) return 'archive';
    if (type === 'application/pdf' || type === 'application/msword'
        || type.startsWith('application/vnd.openxmlformats') || type.startsWith('application/vnd.oasis.opendocument')) {
      return 'document';
    }
    return 'other';
  }

  function fileBadge(name, mimeType) {
    const icon = document.createElement('span');
    icon.className = 'file-icon';
    icon.dataset.kind = fileKind(mimeType);
    icon.textContent = badgeLabel(name);
    icon.setAttribute('aria-hidden', 'true'); // the name next to it says it all
    return icon;
  }

  function folderBadge() {
    const icon = document.createElement('span');
    icon.className = 'file-icon';
    icon.dataset.kind = 'folder';
    icon.setAttribute('aria-hidden', 'true');
    icon.appendChild(cloneTemplate('icon-folder'));
    return icon;
  }

  // ------------------------------------------------------------------ API
  /** fetch() + JSON. On failure, throws an Error carrying the server's
   *  message ({"detail": "..."} from FastAPI) and the HTTP status. */
  async function api(url, options) {
    const response = await fetch(url, options);
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = body && typeof body.detail === 'string' ? body.detail : `Request failed (HTTP ${response.status}).`;
      const error = new Error(detail);
      error.status = response.status;
      throw error;
    }
    return body;
  }

  /** "?folder_id=<id>" for a folder, "" for the root. */
  const query = (name, id) => (id ? `?${name}=${encodeURIComponent(id)}` : '');

  /** POST /api/folders. The JSON content type cannot be sent by another
   *  website without the browser's permission (CORS preflight): like the
   *  upload, folder creation is protected against CSRF by construction. */
  function createFolder(name, parentId, onConflict = 'fail') {
    return api('/api/folders', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, parent_id: parentId, on_conflict: onConflict }),
    });
  }

  // --------------------------------------------------------------- notices
  let noticeTimer = null;

  /** A short, polite message under the drop zone (role="status"). */
  function showNotice(text) {
    noticeEl.textContent = text;
    noticeEl.hidden = false;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => { noticeEl.hidden = true; }, 7000);
  }

  // ------------------------------------------------------------- summary
  /** Totals of the WHOLE cloud (every folder), in the panel header. */
  async function refreshSummary() {
    let summary;
    try {
      summary = await api('/api/files/summary');
    } catch {
      return; // the list shows its own error; keep the previous totals
    }
    if (!summary.files && !summary.folders) {
      summaryEl.textContent = 'No file yet · everything stays on this machine';
    } else {
      const parts = [];
      if (summary.folders) parts.push(plural(summary.folders, 'folder'));
      parts.push(plural(summary.files, 'file'), formatBytes(summary.total_size));
      if (summary.in_ai_memory) parts.push(`${summary.in_ai_memory} in AI memory`);
      summaryEl.textContent = parts.join(' · ');
    }
    document.dispatchEvent(new CustomEvent('cloud:changed', { detail: { summary } }));
  }

  // ----------------------------------------------------------- navigation
  /** Folder id in the address ("#folder=<uuid>"), or null for the root.
   *  Anything that is not a well-formed UUID is ignored (the server would
   *  refuse it anyway, see the typed route parameters). */
  function folderFromAddress() {
    const match = /^#folder=(.+)$/.exec(location.hash);
    return match && UUID.test(match[1]) ? match[1].toLowerCase() : null;
  }

  /** Open a folder (null = the root) and record it in the browser history. */
  function navigate(folderId, { focus = false } = {}) {
    const hash = folderId ? `#folder=${folderId}` : '';
    if (location.hash !== hash) history.pushState(null, '', `${location.pathname}${location.search}${hash}`);
    return loadFolder(folderId, { focus });
  }

  // Back / Forward buttons of the browser.
  window.addEventListener('popstate', () => loadFolder(folderFromAddress(), { focus: true }));

  /** Load and show one folder: its sub-folders, its files and its path. */
  async function loadFolder(folderId, { focus = false } = {}) {
    const token = ++loadToken;
    let subfolders;
    let list;
    let detail = null;
    try {
      // Three independent requests, sent in parallel.
      [subfolders, list, detail] = await Promise.all([
        api(`/api/folders${query('parent_id', folderId)}`),
        api(`/api/files${query('folder_id', folderId)}`),
        folderId ? api(`/api/folders/${encodeURIComponent(folderId)}`) : Promise.resolve(null),
      ]);
    } catch (error) {
      if (token !== loadToken) return;
      if (error.status === 404 && folderId) {
        // Deleted meanwhile, or a stale bookmark: back to the root.
        history.replaceState(null, '', `${location.pathname}${location.search}`);
        showNotice('This folder does not exist any more: back to all your files.');
        await loadFolder(null, { focus });
        return;
      }
      // Keep whatever is on screen; the status dot of the chat shows the
      // server state too.
      errorEl.textContent = 'Could not load your files. Is the server running?';
      errorEl.hidden = false;
      return;
    }
    if (token !== loadToken) return; // the user already went somewhere else

    if (folderId !== currentFolderId) {
      // A whole new listing: no float-in animation for each of its rows,
      // and a half-typed folder name belonged to the previous folder.
      firstRender = true;
      hideFolderForm();
    }
    currentFolderId = folderId;
    currentPath = detail ? detail.path : [];
    folders = subfolders;
    files = list;
    errorEl.hidden = true;
    renderBreadcrumb();
    renderList();
    if (focus) {
      // Where the user arrived: the current step of the path, or the
      // panel's title at the root (the path is hidden there).
      const target = currentPath.length ? breadcrumbEl.querySelector('[aria-current="page"]') : $('files-title');
      target?.focus();
    }
  }

  /** Name of the current folder, for the drop zone and the overlay. */
  const currentPlace = () => (currentPath.length ? `“${currentPath[currentPath.length - 1].name}”` : 'your cloud');

  function renderBreadcrumb() {
    const steps = [{ id: null, name: 'All files' }, ...currentPath];
    const items = steps.map((step, index) => {
      const item = document.createElement('li');
      item.className = 'breadcrumb-step';
      if (index === steps.length - 1) {
        // The current folder: not a link, but the target of the focus after
        // a navigation, so screen readers announce where the user arrived.
        const here = document.createElement('span');
        here.className = 'breadcrumb-current';
        here.setAttribute('aria-current', 'page');
        here.tabIndex = -1;
        here.textContent = step.name;
        here.title = step.name;
        item.appendChild(here);
      } else {
        const link = document.createElement('button');
        link.type = 'button';
        link.className = 'breadcrumb-link';
        link.textContent = step.name;
        link.title = step.name;
        link.addEventListener('click', () => navigate(step.id, { focus: true }));
        item.appendChild(link);
      }
      return item;
    });
    breadcrumbEl.replaceChildren(...items);
    // At the root, "All files" alone would only repeat the panel's title.
    pathEl.hidden = currentPath.length === 0;
    // A long path scrolls sideways: keep its end (where we are) in view.
    pathEl.scrollLeft = pathEl.scrollWidth;
    $('dropzone-target').textContent = currentPlace();
    $('drop-overlay-target').textContent = currentPlace();
  }

  // ---------------------------------------------------------------- list
  /** "View" or "Download" link of a file row. */
  function actionLink({ href, icon, label, accessibleName, newTab = false, download = false }) {
    const link = document.createElement('a');
    link.className = 'file-action';
    link.href = href;
    link.title = label;
    // The accessible name starts with the visible label ("Label in Name",
    // WCAG 2.5.3), then says WHICH file: "Download Rapport.pdf".
    link.setAttribute('aria-label', accessibleName);
    if (newTab) {
      link.target = '_blank';
      link.rel = 'noopener noreferrer'; // the new tab gets no handle on this page
    }
    // The server's Content-Disposition header gives the real file name.
    if (download) link.setAttribute('download', '');
    const text = document.createElement('span');
    text.className = 'file-action-label';
    text.textContent = label;
    link.append(cloneTemplate(icon), text);
    return link;
  }

  /**
   * "Add to AI memory" (RAG preparation). Inactive until v0.7, where it will
   * extract the text, embed it into pgvector and set is_vectorized = true.
   * aria-disabled instead of disabled: the button stays focusable, so it can
   * be discovered with the keyboard, and a click explains why nothing happens.
   */
  function memoryButton(file) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'file-action file-action--memory';
    button.setAttribute('aria-disabled', 'true');
    button.setAttribute('aria-label', `Add to AI memory: ${file.filename} (not available yet)`);
    button.title = 'Add to AI memory: coming soon (the file will be indexed so the AI can use it, RAG)';
    const text = document.createElement('span');
    text.className = 'file-action-label';
    text.textContent = 'Add to AI memory';
    button.append(cloneTemplate('icon-memory'), text);
    button.addEventListener('click', () => {
      showNotice('"Add to AI memory" arrives with RAG (v0.7). For now, uploading only stores your files.');
    });
    return button;
  }

  /** Rows that were not on screen before float in (not the whole list). */
  function markNew(item, id) {
    item.dataset.id = id;
    if (!firstRender && !renderedIds.has(id)) item.classList.add('is-new');
    renderedIds.add(id);
  }

  function renderFolder(folder) {
    const item = document.createElement('li');
    item.className = 'file-item folder-item';
    markNew(item, folder.id);

    // The whole row is one button: a click (or Enter / Space) opens it.
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'folder-open';

    const main = document.createElement('span');
    main.className = 'file-main';
    const name = document.createElement('span');
    name.className = 'file-name';
    name.textContent = folder.name; // untrusted text: never innerHTML
    name.title = folder.name;
    const meta = document.createElement('span');
    meta.className = 'file-meta';
    const kind = document.createElement('span');
    kind.textContent = 'Folder';
    const date = document.createElement('time');
    date.dateTime = folder.created_at;
    date.textContent = formatDate(folder.created_at);
    meta.append(kind, date);
    main.append(name, meta);

    const chevron = document.createElement('span');
    chevron.className = 'folder-chevron';
    chevron.setAttribute('aria-hidden', 'true');

    open.append(folderBadge(), main, chevron);
    open.addEventListener('click', () => navigate(folder.id, { focus: true }));
    item.appendChild(open);
    return item;
  }

  function renderFile(file) {
    const item = document.createElement('li');
    item.className = 'file-item';
    markNew(item, file.id);

    const main = document.createElement('div');
    main.className = 'file-main';
    const name = document.createElement('span');
    name.className = 'file-name';
    name.textContent = file.filename; // untrusted text: never innerHTML
    name.title = file.filename;

    const meta = document.createElement('span');
    meta.className = 'file-meta';
    const size = document.createElement('span');
    size.textContent = formatBytes(file.size);
    const date = document.createElement('time');
    date.dateTime = file.created_at;
    date.textContent = formatDate(file.created_at);
    const badge = document.createElement('span');
    badge.className = `file-badge${file.is_vectorized ? ' is-memory' : ''}`;
    badge.textContent = file.is_vectorized ? 'In AI memory' : 'Cloud only';
    meta.append(size, date, badge);
    main.append(name, meta);

    const actions = document.createElement('div');
    actions.className = 'file-actions';
    const url = `/api/files/${encodeURIComponent(file.id)}/download`;
    // "previewable" is decided by the server's allow-list (images, PDF,
    // audio, video, text): the browser never duplicates that security rule.
    if (file.previewable) {
      actions.appendChild(actionLink({
        href: `${url}?inline=true`, icon: 'icon-eye', label: 'View',
        accessibleName: `View ${file.filename}`, newTab: true,
      }));
    }
    actions.appendChild(actionLink({
      href: url, icon: 'icon-download', label: 'Download',
      accessibleName: `Download ${file.filename}`, download: true,
    }));
    actions.appendChild(memoryButton(file));

    item.append(fileBadge(file.filename, file.mime_type), main, actions);
    return item;
  }

  function updateEmptyState() {
    const empty = !folders.length && !files.length;
    // Not while the "New folder" form or upload rows already fill the panel.
    emptyEl.hidden = !empty || !folderForm.hidden || !queueEl.hidden;
    if (currentFolderId) {
      emptyTitleEl.textContent = 'This folder is empty';
      emptyTextEl.textContent = 'Drop files or folders here, or create a sub-folder.';
    } else {
      emptyTitleEl.textContent = 'Your cloud is still empty';
      emptyTextEl.textContent = 'Drop a first document or folder: it stays on this machine. You will be able to add files to the AI memory later.';
    }
  }

  function renderList() {
    listEl.replaceChildren(...folders.map(renderFolder), ...files.map(renderFile));
    updateEmptyState();
    firstRender = false;
  }

  // ----------------------------------------------------------- new folder
  /** "New folder", or "New folder (2)" if that name is taken here (the
   *  same rule as the server's "rename" mode, so the suggestion is free). */
  function suggestedName() {
    const taken = new Set(folders.map((folder) => folder.name));
    const base = 'New folder';
    if (!taken.has(base)) return base;
    let number = 2;
    while (taken.has(`${base} (${number})`)) number += 1;
    return `${base} (${number})`;
  }

  function showFolderForm() {
    folderForm.hidden = false;
    folderFormError.hidden = true;
    folderNameInput.removeAttribute('aria-invalid');
    folderNameInput.value = suggestedName();
    updateEmptyState();
    // Pre-selected: typing replaces the suggestion, Enter accepts it.
    folderNameInput.focus();
    folderNameInput.select();
  }

  function hideFolderForm({ restoreFocus = false } = {}) {
    if (folderForm.hidden) return;
    folderForm.hidden = true;
    updateEmptyState();
    if (restoreFocus) newFolderBtn.focus();
  }

  function showFolderFormError(message) {
    folderFormError.textContent = message;
    folderFormError.hidden = false;
    folderNameInput.setAttribute('aria-invalid', 'true');
    folderNameInput.focus();
  }

  folderForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const name = folderNameInput.value.trim();
    if (!name) {
      showFolderFormError('Give the folder a name.');
      return;
    }
    folderCreateBtn.disabled = true;
    try {
      const folder = await createFolder(name, currentFolderId);
      hideFolderForm();
      // Reload rather than insert locally: the server's order is the truth.
      await loadFolder(currentFolderId);
      refreshSummary();
      listEl.querySelector(`[data-id="${CSS.escape(folder.id)}"] .folder-open`)?.focus();
    } catch (error) {
      // 409 "already exists", 422 "cannot contain /"...: the server's words.
      showFolderFormError(error.message);
    } finally {
      folderCreateBtn.disabled = false;
    }
  });

  folderNameInput.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      hideFolderForm({ restoreFocus: true });
    }
  });
  folderNameInput.addEventListener('input', () => {
    folderFormError.hidden = true;
    folderNameInput.removeAttribute('aria-invalid');
  });
  folderForm.querySelector('.file-icon').appendChild(cloneTemplate('icon-folder'));
  $('folder-cancel').appendChild(cloneTemplate('icon-close'));
  $('folder-cancel').addEventListener('click', () => hideFolderForm({ restoreFocus: true }));
  newFolderBtn.addEventListener('click', () => {
    if (folderForm.hidden) showFolderForm();
    else folderNameInput.focus();
  });

  // ------------------------------------------------------- upload queue
  /*
   * A "job" is what the user added in one gesture and what shows as ONE row
   * of the queue: a single file, or a whole folder. A job holds "tasks", one
   * per file to send, each with its destination folder.
   */
  function createJob({ kind, label, targetId, tasks = [], totalBytes = 0, fileCount = 0 }) {
    const row = document.createElement('li');
    row.className = 'upload-item';

    const main = document.createElement('div');
    main.className = 'upload-main';
    const name = document.createElement('span');
    name.className = 'file-name';
    name.textContent = label;
    name.title = label;
    // role="progressbar" + aria-valuenow: screen readers can announce the progress.
    const progress = document.createElement('div');
    progress.className = 'progress';
    progress.setAttribute('role', 'progressbar');
    progress.setAttribute('aria-valuemin', '0');
    progress.setAttribute('aria-valuemax', '100');
    progress.setAttribute('aria-valuenow', '0');
    progress.setAttribute('aria-label', `Uploading ${label}`);
    const bar = document.createElement('span');
    bar.className = 'progress-bar';
    progress.appendChild(bar);
    const status = document.createElement('span');
    status.className = 'upload-status';
    main.append(name, progress, status);

    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'file-action';
    cancel.setAttribute('aria-label', `Cancel the upload of ${label}`);
    cancel.title = 'Cancel';
    cancel.appendChild(cloneTemplate('icon-close'));

    const badge = kind === 'folder' ? folderBadge() : fileBadge(label, tasks[0]?.file.type);
    row.append(badge, main, cancel);
    queueEl.appendChild(row);
    queueEl.hidden = false;
    emptyEl.hidden = true;

    const job = {
      kind, label, targetId, tasks, totalBytes, fileCount,
      row, name, bar, progress, status, cancel,
      plan: null,        // folder jobs: the tree to recreate
      failures: [],      // [{path, error}]
      sentBytes: 0,      // bytes of the files already sent
      uploaded: 0,       // number of files saved
      current: 0,        // position of the file being sent (1-based)
      xhr: null, running: false, cancelled: false, done: false,
    };
    cancel.addEventListener('click', () => cancelJob(job));
    return job;
  }

  /** "Waiting" line: size, and number of files for a folder. */
  function showWaiting(job) {
    job.status.textContent = job.kind === 'folder'
      ? `Waiting · ${plural(job.fileCount, 'file')} · ${formatBytes(job.totalBytes)}`
      : `Waiting · ${formatBytes(job.totalBytes)}`;
  }

  function removeJobRow(job) {
    job.row.remove();
    queueEl.hidden = queueEl.children.length === 0;
    updateEmptyState();
  }

  function cancelJob(job) {
    if (job.done) {            // an error row: "cancel" just dismisses it
      removeJobRow(job);
      return;
    }
    job.cancelled = true;
    if (job.xhr) {
      // The server sees the connection close (ClientDisconnect) and deletes
      // the partial file: nothing half-written stays on the disk.
      job.xhr.abort();
      return;
    }
    if (job.running) return;   // between two files: runJob() sees the flag
    const index = pending.indexOf(job);
    if (index !== -1) pending.splice(index, 1);
    removeJobRow(job);         // still waiting (or still being read): just drop it
  }

  function setProgress(job, currentLoaded) {
    const loaded = job.sentBytes + currentLoaded;
    const ratio = job.totalBytes > 0 ? Math.min(loaded / job.totalBytes, 1) : 1;
    const percent = Math.round(ratio * 100);
    job.bar.style.setProperty('--progress', ratio.toFixed(4));
    job.progress.setAttribute('aria-valuenow', String(percent));
    const amount = `${percent}% · ${formatBytes(loaded)} of ${formatBytes(job.totalBytes)}`;
    job.status.textContent = job.kind === 'folder'
      ? `File ${job.current} of ${job.fileCount} · ${amount}`
      : amount;
  }

  function failJob(job, message) {
    job.done = true;
    job.row.classList.remove('is-finishing');
    job.row.classList.add('is-error');
    job.status.textContent = message;
    job.status.setAttribute('role', 'alert');
    job.progress.remove();
    job.cancel.setAttribute('aria-label', `Dismiss the error about ${job.label}`);
    job.cancel.title = 'Dismiss';
  }

  /** Human message for a failed request ({"detail": "..."} from FastAPI). */
  function uploadError(xhr) {
    const detail = xhr.response && xhr.response.detail;
    return typeof detail === 'string' ? detail : `Upload failed (HTTP ${xhr.status}).`;
  }

  /**
   * Send one file. Resolves (never rejects) with "ok", "failed", "network"
   * (the server could not be reached: no point trying the next files) or
   * "aborted" (the user cancelled).
   */
  function sendFile(job, task) {
    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      job.xhr = xhr;
      xhr.open('POST', `/api/files${query('folder_id', task.folderId)}`);
      xhr.responseType = 'json';
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      // Headers are ASCII-only: the name travels percent-encoded (UTF-8).
      xhr.setRequestHeader('X-File-Name', encodeURIComponent(task.file.name));

      xhr.upload.addEventListener('progress', (event) => {
        if (event.lengthComputable) setProgress(job, event.loaded);
      });
      // Every byte has left the browser; the server now writes to disk,
      // flushes (fsync) and indexes the file.
      xhr.upload.addEventListener('load', () => {
        setProgress(job, task.file.size);
        if (job.kind === 'file') {
          job.row.classList.add('is-finishing');
          job.status.textContent = 'Saving on your server…';
        }
      });
      xhr.addEventListener('load', () => {
        job.xhr = null;
        if (xhr.status === 201 && xhr.response) {
          job.sentBytes += task.file.size;
          job.uploaded += 1;
          // Shown at once if the user is looking at its folder.
          if (task.folderId === currentFolderId) {
            files.unshift(xhr.response);
            renderList();
          }
          resolve('ok');
        } else {
          job.sentBytes += task.file.size; // keeps the global bar moving
          job.failures.push({ path: task.path, error: uploadError(xhr) });
          resolve('failed');
        }
      });
      xhr.addEventListener('error', () => {
        job.xhr = null;
        job.failures.push({
          path: task.path,
          error: 'Network error: the server could not be reached, or it closed the connection.',
        });
        resolve('network');
      });
      xhr.addEventListener('abort', () => {
        job.xhr = null;
        resolve('aborted');
      });

      // A File is a Blob: the browser streams it from the disk, it is never
      // loaded into the page's memory.
      xhr.send(task.file);
    });
  }

  /**
   * Recreate the folder tree of a folder job in the cloud, parents first,
   * and give every file its destination folder id. Returns false if the
   * top folder itself could not be created (nothing else is attempted).
   */
  async function createTree(job) {
    const { plan } = job;
    const ids = new Map(); // path in the dropped tree -> id of the folder in the cloud
    for (const [index, dir] of plan.dirs.entries()) {
      if (job.cancelled) return false;
      job.status.textContent = `Creating folders · ${index + 1} of ${plan.dirs.length}`;
      const slash = dir.lastIndexOf('/');
      const parentPath = slash === -1 ? null : dir.slice(0, slash);
      const parentId = parentPath === null ? job.targetId : ids.get(parentPath);
      if (parentId === undefined) continue; // its parent failed: already reported
      try {
        // "rename": an existing "Photos" makes this one "Photos (2)".
        const folder = await createFolder(dir.slice(slash + 1), parentId, 'rename');
        ids.set(dir, folder.id);
        if (parentPath === null && folder.name !== job.label) {
          job.label = folder.name;
          job.name.textContent = folder.name;
          job.name.title = folder.name;
        }
      } catch (error) {
        job.failures.push({ path: dir, error: error.message });
        if (parentPath === null) return false;
      }
    }
    job.tasks = [];
    for (const { file, path } of plan.files) {
      const folderId = ids.get(path.slice(0, path.lastIndexOf('/')));
      if (folderId) job.tasks.push({ file, path, folderId });
      else job.failures.push({ path, error: 'Its folder could not be created.' });
    }
    return true;
  }

  async function runJob(job) {
    job.running = true;
    if (job.kind === 'folder') {
      if (!(await createTree(job))) {
        finishJob(job);
        return;
      }
      // The new top folder appears at once if the user is looking at its parent.
      if (job.targetId === currentFolderId) loadFolder(currentFolderId);
    }
    for (const [index, task] of job.tasks.entries()) {
      if (job.cancelled) break;
      job.current = index + 1;
      setProgress(job, 0);
      const outcome = await sendFile(job, task);
      if (outcome === 'aborted' || outcome === 'network') break;
    }
    finishJob(job);
  }

  function finishJob(job) {
    job.running = false;
    if (job.cancelled) {
      removeJobRow(job);
      if (job.kind === 'folder') {
        showNotice(`Upload of “${job.label}” cancelled. ${plural(job.uploaded, 'file')} had already been saved.`);
      }
      return;
    }
    if (!job.failures.length && job.uploaded === job.tasks.length) {
      removeJobRow(job);
      if (job.kind === 'folder') {
        showNotice(job.uploaded
          ? `Folder “${job.label}” uploaded: ${plural(job.uploaded, 'file')}.`
          : `Folder “${job.label}” created (it contains no file).`);
      }
      return;
    }
    const [first] = job.failures;
    if (job.kind === 'file') {
      failJob(job, first ? first.error : 'Upload failed.');
    } else if (!first) {
      failJob(job, 'The upload stopped before the end.');
    } else {
      const notSent = job.tasks.length - job.uploaded - job.failures.length;
      const count = job.failures.length + Math.max(notSent, 0);
      failJob(job, `${plural(count, 'item')} could not be uploaded. ${first.path}: ${first.error}`);
    }
  }

  async function processQueue() {
    if (uploading) return;
    uploading = true;
    while (pending.length) {
      await runJob(pending.shift());
    }
    uploading = false;
    // Final consistency check with the server (another tab may have
    // uploaded too), new sub-folders included; also restores the empty state.
    await loadFolder(currentFolderId);
    refreshSummary();
  }

  function enqueue(job) {
    showWaiting(job);
    pending.push(job);
    processQueue();
  }

  /** Loose files: one job each, into ``targetId``. */
  function enqueueFiles(fileList, targetId) {
    for (const file of fileList) {
      enqueue(createJob({
        kind: 'file', label: file.name, targetId,
        tasks: [{ file, path: file.name, folderId: targetId }],
        totalBytes: file.size, fileCount: 1,
      }));
    }
  }

  /** A folder whose tree is known (``plan``): one job for all of it. */
  function enqueueFolder(job, plan) {
    job.plan = plan;
    job.fileCount = plan.files.length;
    job.totalBytes = plan.files.reduce((sum, item) => sum + item.file.size, 0);
    for (const path of plan.unreadable) {
      job.failures.push({ path, error: 'This file could not be read from your computer.' });
    }
    enqueue(job);
  }

  // -------------------------------------------------- reading folder trees
  class TooManyFilesError extends Error {}

  /**
   * Folders picked with the folder picker: the browser gives a flat list of
   * files, each with its path ("Photos/2026/beach.jpg"). Rebuild the list
   * of folders from those paths, grouped by top folder (one job each).
   * Parents always come before their children in ``dirs``. Empty
   * sub-folders are not reported by the picker, so they are not recreated.
   */
  function plansFromPaths(fileList) {
    const plans = new Map();
    for (const file of fileList) {
      const parts = (file.webkitRelativePath || file.name).split('/');
      if (IGNORED_FILES.has(parts[parts.length - 1])) continue;
      const root = parts[0];
      if (!plans.has(root)) plans.set(root, { root, dirs: new Set(), files: [], unreadable: [] });
      const plan = plans.get(root);
      for (let depth = 1; depth < parts.length; depth += 1) plan.dirs.add(parts.slice(0, depth).join('/'));
      plan.files.push({ file, path: parts.join('/') });
    }
    return [...plans.values()].map((plan) => ({ ...plan, dirs: [...plan.dirs] }));
  }

  /**
   * A folder dropped from the computer (FileSystemDirectoryEntry): walk it
   * recursively. Unlike the picker, this also sees empty sub-folders.
   * Parents are visited before their children (pre-order), so ``dirs`` is
   * already in creation order.
   */
  async function readDroppedFolder(rootEntry) {
    const plan = { root: rootEntry.name, dirs: [], files: [], unreadable: [] };

    async function walk(dirEntry, path) {
      plan.dirs.push(path);
      const reader = dirEntry.createReader();
      // readEntries() hands the content over in batches (100 entries at a
      // time in Chromium): it must be called again until a batch is empty.
      for (;;) {
        const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
        if (!batch.length) break;
        for (const entry of batch) {
          const childPath = `${path}/${entry.name}`;
          if (entry.isDirectory) {
            await walk(entry, childPath);
          } else if (entry.isFile && !IGNORED_FILES.has(entry.name)) {
            if (plan.files.length >= MAX_FOLDER_UPLOAD_FILES) throw new TooManyFilesError();
            try {
              plan.files.push({ file: await new Promise((resolve, reject) => entry.file(resolve, reject)), path: childPath });
            } catch {
              plan.unreadable.push(childPath); // e.g. permission denied
            }
          }
        }
      }
    }

    await walk(rootEntry, rootEntry.name);
    return plan;
  }

  const tooManyMessage = () => `More than ${new Intl.NumberFormat(locale).format(MAX_FOLDER_UPLOAD_FILES)} files: please upload this folder in smaller parts.`;

  /** Show the row at once ("Reading the folder…"), then read and enqueue. */
  async function enqueueDroppedFolder(entry, targetId) {
    const job = createJob({ kind: 'folder', label: entry.name, targetId });
    job.status.textContent = 'Reading the folder…';
    let plan;
    try {
      plan = await readDroppedFolder(entry);
    } catch (error) {
      if (!job.cancelled) failJob(job, error instanceof TooManyFilesError ? tooManyMessage() : 'This folder could not be read.');
      return;
    }
    if (job.cancelled) return; // cancelled while it was being read
    enqueueFolder(job, plan);
  }

  // --------------------------------------------------------- drag & drop
  // Only react to drags that carry files (not text or a link dragged from
  // the page itself).
  const carriesFiles = (event) => [...(event.dataTransfer?.types || [])].includes('Files');

  // dragenter/dragleave fire for every child element crossed by the
  // pointer: a counter tells when the pointer has really left the panel.
  let dragDepth = 0;

  panel.addEventListener('dragenter', (event) => {
    if (!carriesFiles(event)) return;
    event.preventDefault();
    dragDepth += 1;
    panelInner.classList.add('is-dragover');
  });

  panel.addEventListener('dragover', (event) => {
    if (!carriesFiles(event)) return;
    event.preventDefault(); // without this, the browser refuses the drop
    event.dataTransfer.dropEffect = 'copy';
  });

  panel.addEventListener('dragleave', (event) => {
    if (!carriesFiles(event)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) panelInner.classList.remove('is-dragover');
  });

  panel.addEventListener('drop', (event) => {
    if (!carriesFiles(event)) return;
    event.preventDefault();
    dragDepth = 0;
    panelInner.classList.remove('is-dragover');

    // Everything must be taken out of dataTransfer NOW, synchronously: the
    // browser empties it as soon as this handler returns. The folder
    // entries obtained here stay readable afterwards.
    const target = currentFolderId;
    const looseFiles = [];
    const folderEntries = [];
    const items = [...(event.dataTransfer.items || [])];
    if (items.length) {
      for (const item of items) {
        if (item.kind !== 'file') continue;
        const entry = typeof item.webkitGetAsEntry === 'function' ? item.webkitGetAsEntry() : null;
        if (entry && entry.isDirectory) {
          folderEntries.push(entry);
        } else {
          const file = item.getAsFile();
          if (file) looseFiles.push(file);
        }
      }
    } else {
      looseFiles.push(...event.dataTransfer.files);
    }
    if (looseFiles.length) enqueueFiles(looseFiles, target);
    for (const entry of folderEntries) enqueueDroppedFolder(entry, target);
  });

  // A file dropped OUTSIDE the panel would make the browser open it and
  // leave the application (an unsaved answer would be lost): cancel that
  // default everywhere, and show a "forbidden" cursor outside the panel.
  window.addEventListener('dragover', (event) => {
    if (!carriesFiles(event)) return;
    event.preventDefault();
    if (!panel.contains(event.target)) event.dataTransfer.dropEffect = 'none';
  });
  window.addEventListener('drop', (event) => {
    if (carriesFiles(event)) event.preventDefault();
  });

  // ------------------------------------------------------ picker buttons
  $('upload-btn').addEventListener('click', () => fileInput.click());
  $('dropzone').addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => {
    if (fileInput.files.length) enqueueFiles([...fileInput.files], currentFolderId);
    fileInput.value = ''; // so that choosing the same file again still fires "change"
  });

  // The folder picker exists in every desktop browser, not on phones.
  if (!('webkitdirectory' in folderInput)) uploadFolderBtn.hidden = true;
  uploadFolderBtn.addEventListener('click', () => folderInput.click());
  folderInput.addEventListener('change', () => {
    const picked = [...folderInput.files];
    folderInput.value = '';
    if (!picked.length) {
      // Browsers do not report empty folders: nothing to send.
      showNotice('This folder is empty: there is nothing to upload. Use "New folder" to create an empty one.');
      return;
    }
    if (picked.length > MAX_FOLDER_UPLOAD_FILES) {
      showNotice(tooManyMessage());
      return;
    }
    for (const plan of plansFromPaths(picked)) {
      enqueueFolder(createJob({ kind: 'folder', label: plan.root, targetId: currentFolderId }), plan);
    }
  });

  // Closing the tab during an upload would cancel it: ask first.
  window.addEventListener('beforeunload', (event) => {
    if (uploading) event.preventDefault();
  });

  // ----------------------------------------------------------------- init
  loadFolder(folderFromAddress());
  refreshSummary();
})();
