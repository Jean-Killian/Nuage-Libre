/*
 * Nuage Libre: file manager of the private cloud (left panel).
 *
 * Responsibilities: list the user's files (GET /api/files), upload new ones
 * by drag & drop or with the file picker (POST /api/files), and offer
 * "View" / "Download" links (GET /api/files/{id}/download).
 *
 * Upload protocol (see app/routers/files.py for the reasons): the body of
 * the request IS the file, not a multipart form.
 *
 *     POST /api/files
 *     Content-Type: application/octet-stream
 *     X-File-Name: <encodeURIComponent(file.name)>
 *     <raw bytes>
 *
 * XMLHttpRequest is used instead of fetch() for one reason: fetch() cannot
 * report UPLOAD progress, and a progress bar matters for a 2 GB video.
 * Files are sent one at a time (a queue): uploads do not compete for the
 * bandwidth and the disk, and each one shows a meaningful progress.
 *
 * Security: file names come from the user, and once stored they come back
 * from the server; they are only ever inserted with `textContent`, never
 * interpreted as HTML (see the security rules at the top of js/app.js).
 *
 * Other scripts are told when the list changes through a "files:changed"
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
  const errorEl = $('files-error');
  const noticeEl = $('files-notice');
  const summaryEl = $('files-summary');
  const fileInput = $('file-input');

  /** Return a fresh copy of a <template>'s content (icon, mascot...). */
  const cloneTemplate = (id) => $(id).content.firstElementChild.cloneNode(true);

  // Dates and numbers follow the language of the page (<html lang="en">).
  const locale = document.documentElement.lang || undefined;

  // ---------------------------------------------------------------- state
  let files = [];                // FileOut objects, newest first (from the API)
  const renderedIds = new Set(); // ids already on screen: only NEW rows animate
  let firstRender = true;
  const pending = [];            // uploads waiting for their turn
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

  // --------------------------------------------------------------- notices
  let noticeTimer = null;

  /** A short, polite message under the drop zone (role="status"). */
  function showNotice(text) {
    noticeEl.textContent = text;
    noticeEl.hidden = false;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => { noticeEl.hidden = true; }, 6000);
  }

  // ---------------------------------------------------------------- list
  function updateSummary() {
    if (!files.length) {
      summaryEl.textContent = 'No file yet · everything stays on this machine';
      return;
    }
    const total = files.reduce((sum, f) => sum + f.size, 0);
    const inMemory = files.filter((f) => f.is_vectorized).length;
    const parts = [`${files.length} ${files.length === 1 ? 'file' : 'files'}`, formatBytes(total)];
    if (inMemory) parts.push(`${inMemory} in AI memory`);
    summaryEl.textContent = parts.join(' · ');
  }

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

  function renderFile(file) {
    const item = document.createElement('li');
    item.className = 'file-item';
    if (!firstRender && !renderedIds.has(file.id)) item.classList.add('is-new');
    renderedIds.add(file.id);

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

  function renderList() {
    listEl.replaceChildren(...files.map(renderFile));
    emptyEl.hidden = files.length > 0 || pending.length > 0 || uploading;
    updateSummary();
    firstRender = false;
  }

  function notifyChange() {
    document.dispatchEvent(new CustomEvent('files:changed', { detail: { files: files.slice() } }));
  }

  async function refreshFiles() {
    try {
      const response = await fetch('/api/files');
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      files = await response.json();
      errorEl.hidden = true;
    } catch {
      // Keep whatever is on screen; the status dot of the chat shows the
      // server state too.
      errorEl.textContent = 'Could not load your files. Is the server running?';
      errorEl.hidden = false;
      return;
    }
    renderList();
    notifyChange();
  }

  // --------------------------------------------------------------- upload
  function createQueueItem(file) {
    const row = document.createElement('li');
    row.className = 'upload-item';

    const main = document.createElement('div');
    main.className = 'upload-main';
    const name = document.createElement('span');
    name.className = 'file-name';
    name.textContent = file.name;
    name.title = file.name;
    // role="progressbar" + aria-valuenow: screen readers can announce the progress.
    const progress = document.createElement('div');
    progress.className = 'progress';
    progress.setAttribute('role', 'progressbar');
    progress.setAttribute('aria-valuemin', '0');
    progress.setAttribute('aria-valuemax', '100');
    progress.setAttribute('aria-valuenow', '0');
    progress.setAttribute('aria-label', `Uploading ${file.name}`);
    const bar = document.createElement('span');
    bar.className = 'progress-bar';
    progress.appendChild(bar);
    const status = document.createElement('span');
    status.className = 'upload-status';
    status.textContent = `Waiting · ${formatBytes(file.size)}`;
    main.append(name, progress, status);

    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'file-action';
    cancel.setAttribute('aria-label', `Cancel the upload of ${file.name}`);
    cancel.title = 'Cancel';
    cancel.appendChild(cloneTemplate('icon-close'));

    row.append(fileBadge(file.name, file.type), main, cancel);
    queueEl.appendChild(row);
    queueEl.hidden = false;

    const item = { file, row, bar, progress, status, cancel, xhr: null, done: false };
    cancel.addEventListener('click', () => cancelItem(item));
    return item;
  }

  function removeItem(item) {
    item.row.remove();
    queueEl.hidden = queueEl.children.length === 0;
  }

  function cancelItem(item) {
    if (item.done) {             // an error row: "cancel" just dismisses it
      removeItem(item);
      return;
    }
    if (item.xhr) {
      // The server sees the connection close (ClientDisconnect) and deletes
      // the partial file: nothing half-written stays on the disk.
      item.xhr.abort();
      return;
    }
    const index = pending.indexOf(item); // not started yet: just drop it
    if (index !== -1) pending.splice(index, 1);
    removeItem(item);
  }

  function setProgress(item, loaded, total) {
    const ratio = total > 0 ? loaded / total : 1;
    const percent = Math.round(ratio * 100);
    item.bar.style.setProperty('--progress', ratio.toFixed(4));
    item.progress.setAttribute('aria-valuenow', String(percent));
    item.status.textContent = `${percent}% · ${formatBytes(loaded)} of ${formatBytes(total)}`;
  }

  function failItem(item, message) {
    item.done = true;
    item.xhr = null;
    item.row.classList.remove('is-finishing');
    item.row.classList.add('is-error');
    item.status.textContent = message;
    item.status.setAttribute('role', 'alert');
    item.progress.remove();
    item.cancel.setAttribute('aria-label', `Dismiss the error about ${item.file.name}`);
    item.cancel.title = 'Dismiss';
  }

  /** Human message for a failed request ({"detail": "..."} from FastAPI). */
  function errorMessage(xhr) {
    const detail = xhr.response && xhr.response.detail;
    return typeof detail === 'string' ? detail : `Upload failed (HTTP ${xhr.status}).`;
  }

  /** Upload one file; the promise always resolves (errors are shown in its row). */
  function uploadOne(item) {
    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      item.xhr = xhr;
      xhr.open('POST', '/api/files');
      xhr.responseType = 'json';
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      // Headers are ASCII-only: the name travels percent-encoded (UTF-8).
      xhr.setRequestHeader('X-File-Name', encodeURIComponent(item.file.name));

      xhr.upload.addEventListener('progress', (event) => {
        if (event.lengthComputable) setProgress(item, event.loaded, event.total);
      });
      // Every byte has left the browser; the server now writes to disk,
      // flushes (fsync) and indexes the file.
      xhr.upload.addEventListener('load', () => {
        setProgress(item, item.file.size, item.file.size);
        item.row.classList.add('is-finishing');
        item.status.textContent = 'Saving on your server…';
      });
      xhr.addEventListener('load', () => {
        if (xhr.status === 201 && xhr.response) {
          files.unshift(xhr.response);
          removeItem(item);
          renderList();
          notifyChange();
        } else {
          failItem(item, errorMessage(xhr));
        }
        resolve();
      });
      xhr.addEventListener('error', () => {
        failItem(item, 'Network error: the server could not be reached, or it closed the connection.');
        resolve();
      });
      xhr.addEventListener('abort', () => {
        removeItem(item);
        resolve();
      });

      item.status.textContent = 'Starting…';
      // A File is a Blob: the browser streams it from the disk, it is never
      // loaded into the page's memory.
      xhr.send(item.file);
    });
  }

  async function processQueue() {
    if (uploading) return;
    uploading = true;
    while (pending.length) {
      await uploadOne(pending.shift());
    }
    uploading = false;
    // Final consistency check with the server (another tab may have
    // uploaded too). Also brings the empty state back if needed.
    await refreshFiles();
  }

  function enqueue(fileList) {
    for (const file of fileList) pending.push(createQueueItem(file));
    emptyEl.hidden = true;
    processQueue();
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

    // Folders appear in the drop as File objects that cannot be read:
    // detect and skip them. This must happen synchronously, during the
    // event: the browser empties dataTransfer right after.
    const accepted = [];
    let folders = 0;
    const items = [...(event.dataTransfer.items || [])];
    if (items.length) {
      for (const item of items) {
        if (item.kind !== 'file') continue;
        const entry = typeof item.webkitGetAsEntry === 'function' ? item.webkitGetAsEntry() : null;
        if (entry && entry.isDirectory) {
          folders += 1;
          continue;
        }
        const file = item.getAsFile();
        if (file) accepted.push(file);
      }
    } else {
      accepted.push(...event.dataTransfer.files);
    }
    if (folders) {
      showNotice(`${folders === 1 ? 'A folder was' : `${folders} folders were`} skipped: drop the files themselves (folders are not supported yet).`);
    }
    if (accepted.length) enqueue(accepted);
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
    if (fileInput.files.length) enqueue([...fileInput.files]);
    fileInput.value = ''; // so that choosing the same file again still fires "change"
  });

  // Closing the tab during an upload would cancel it: ask first.
  window.addEventListener('beforeunload', (event) => {
    if (uploading) event.preventDefault();
  });

  // ----------------------------------------------------------------- init
  refreshFiles();
})();
