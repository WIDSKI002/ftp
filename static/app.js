const state = {
  csrf: "",
  username: "admin",
  view: "files",
  provider: "all",
  parents: { google: "root", mega: "/" },
  currentProvider: "all",
  files: [],
  notes: [],
  selectedNote: null,
  saveTimer: null,
  query: "",
  theme: localStorage.getItem("cloudvault-theme") || "light",
};

const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));

async function api(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  if (options.method && !["GET", "HEAD"].includes(options.method.toUpperCase())) headers.set("X-CSRF-Token", state.csrf);
  const response = await fetch(url, { ...options, headers, credentials: "same-origin" });
  const type = response.headers.get("content-type") || "";
  const result = type.includes("application/json") ? await response.json() : await response.blob();
  if (!response.ok) throw new Error(result.error || `Błąd serwera (${response.status}).`);
  if (result.csrf_token) state.csrf = result.csrf_token;
  return result;
}

function toast(message, error = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("error", error);
  el.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("show"), 3400);
}

function setView(view) {
  state.view = view;
  document.querySelectorAll(".view-panel").forEach((el) => el.classList.add("hidden"));
  $(`#${view}-view`)?.classList.remove("hidden");
  document.querySelectorAll(".nav-item[data-view]").forEach((el) => el.classList.toggle("active", el.dataset.view === view));
  $(".search-box:not(.note-search)").classList.toggle("hidden", view !== "files" && view !== "favorites");
  if (view === "favorites") {
    state.currentProvider = "all";
    state.provider = "all";
  }
  if (view === "files" || view === "favorites") loadFiles();
  if (view === "notes") loadNotes();
}

function prettySize(bytes) {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = -1;
  do { value /= 1024; unit += 1; } while (value >= 1024 && unit < units.length - 1);
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unit]}`;
}

function prettyDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat("pl-PL", { dateStyle: "medium" }).format(date);
}

function fileIcon(file) {
  if (file.is_folder) return '<span class="file-icon folder-icon">▰</span>';
  const ext = file.name.split(".").pop().toLowerCase();
  const cls = ["pdf", "jpg", "jpeg", "png", "zip", "doc", "docx", "xls", "xlsx"].includes(ext) ? ` file-icon-${ext}` : "";
  return `<span class="file-icon${cls}">${ext === "pdf" ? "P" : ext.match(/jpg|jpeg|png/) ? "▧" : ext === "zip" ? "Z" : "▤"}</span>`;
}

function renderFiles() {
  let files = [...state.files];
  const filter = $("#file-filter").value;
  if (filter === "folder") files = files.filter((file) => file.is_folder);
  if (filter === "file") files = files.filter((file) => !file.is_folder);
  const query = state.query.toLocaleLowerCase("pl");
  if (query) files = files.filter((file) => file.name.toLocaleLowerCase("pl").includes(query));
  const sort = $("#file-sort").value;
  files.sort((a, b) => {
    if (sort === "size") return (b.size || 0) - (a.size || 0);
    if (sort === "modified") return String(b.modified || "").localeCompare(String(a.modified || ""));
    return Number(b.is_folder) - Number(a.is_folder) || a.name.localeCompare(b.name, "pl", { sensitivity: "base" });
  });
  $("#file-count").textContent = files.length ? `· ${files.length}` : "";
  $("#file-list").innerHTML = files.map(fileRow).join("");
  $("#empty-files").classList.toggle("hidden", files.length > 0);
  $("#file-list").querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const file = files.find((item) => item.id === button.dataset.id && item.provider === button.dataset.provider);
      if (file) handleFileAction(button.dataset.action, file);
    });
  });
  $("#file-list").querySelectorAll("tr[data-open]").forEach((row) => row.addEventListener("dblclick", () => {
    const file = files.find((item) => item.id === row.dataset.id && item.provider === row.dataset.provider);
    if (file?.is_folder) openFolder(file);
  }));
}

function fileRow(file) {
  const providerName = file.provider === "google" ? "Google Drive" : "MEGA";
  const meta = file.is_folder ? "Folder" : prettySize(file.size);
  return `<tr data-open="true" data-id="${escapeHtml(file.id)}" data-provider="${file.provider}">
    <td><div class="file-name-cell">${fileIcon(file)}<span><strong>${escapeHtml(file.name)}</strong><small>${meta}</small></span></div></td>
    <td><span class="provider-pill"><i class="provider-dot ${file.provider === "google" ? "google-dot" : "mega-dot"}"></i>${providerName}</span></td>
    <td>${file.is_folder ? "—" : prettySize(file.size)}</td><td>${prettyDate(file.modified)}</td>
    <td><div class="row-actions"><button class="row-action ${file.favorite ? "is-favorite" : ""}" data-action="favorite" data-id="${escapeHtml(file.id)}" data-provider="${file.provider}" title="Ulubione" aria-label="Ulubione">${file.favorite ? "★" : "☆"}</button><button class="row-action" data-action="menu" data-id="${escapeHtml(file.id)}" data-provider="${file.provider}" title="Więcej opcji" aria-label="Więcej opcji">···</button></div></td>
  </tr>`;
}

async function loadFiles() {
  const selected = state.currentProvider === "all" ? state.provider : state.currentProvider;
  const parent = selected === "all" ? "" : state.parents[selected];
  const params = new URLSearchParams({ provider: selected });
  if (parent) params.set("parent", parent);
  try {
    const data = await api(`/api/files?${params}`);
    state.files = data.files;
    renderFiles();
    updateBreadcrumb();
    if (state.view === "favorites") renderFavorites();
  } catch (error) {
    state.files = [];
    renderFiles();
    toast(error.message, true);
  }
}

function renderFavorites() {
  const favorites = state.files.filter((file) => file.favorite);
  $("#favorites-list").innerHTML = favorites.map(fileRow).join("");
  $("#empty-favorites").classList.toggle("hidden", favorites.length > 0);
  $("#favorites-list").querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => {
    const file = favorites.find((item) => item.id === button.dataset.id && item.provider === button.dataset.provider);
    if (file) handleFileAction(button.dataset.action, file);
  }));
}

function openFolder(file) {
  state.currentProvider = file.provider;
  state.parents[file.provider] = file.id;
  state.provider = file.provider;
  document.querySelectorAll(".chip").forEach((chip) => chip.classList.toggle("active", chip.dataset.provider === file.provider));
  setView("files");
}

function updateBreadcrumb() {
  const provider = state.currentProvider;
  const folder = provider === "all" ? "" : state.parents[provider];
  const title = provider === "all" ? "Moje pliki" : provider === "google" ? "Google Drive" : "MEGA";
  const isRoot = !folder || folder === "root" || folder === "/";
  $("#breadcrumbs").innerHTML = `<button class="breadcrumb-home">${title}</button>${isRoot ? "" : `<span> / </span><span>${escapeHtml(folder.split("/").filter(Boolean).pop() || "Folder")}</span>`}`;
  $(".breadcrumb-home").addEventListener("click", () => {
    if (provider !== "all") state.parents[provider] = provider === "google" ? "root" : "/";
    state.currentProvider = "all";
    state.provider = "all";
    document.querySelectorAll(".chip").forEach((chip) => chip.classList.toggle("active", chip.dataset.provider === "all"));
    loadFiles();
  });
}

async function handleFileAction(action, file) {
  if (action === "favorite") {
    try {
      await api("/api/favorites", { method: file.favorite ? "DELETE" : "PUT", body: JSON.stringify({ provider: file.provider, id: file.id }) });
      file.favorite = !file.favorite;
      renderFiles();
      if (state.view === "favorites") renderFavorites();
    } catch (error) { toast(error.message, true); }
    return;
  }
  if (action === "menu") {
    const dialog = $("#file-actions-dialog");
    const actions = file.is_folder
      ? [["open", "Otwórz folder"], ["rename", "Zmień nazwę"], ["move", "Przenieś"], ["delete", "Usuń"]]
      : [["download", "Pobierz"], ["rename", "Zmień nazwę"], ["move", "Przenieś"], ["delete", "Usuń"]];
    $("#file-actions-title").textContent = file.name;
    $("#file-actions-list").innerHTML = actions.map(([name, label]) =>
      `<button class="file-action-option ${name === "delete" ? "danger-option" : ""}" type="button" data-action="${name}">${label}</button>`
    ).join("");
    $("#file-actions-list").querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => {
      dialog.close();
      handleFileAction(button.dataset.action, file);
    }));
    dialog.showModal();
    return;
  }
  try {
    if (action === "download" && !file.is_folder) {
      const url = `/api/files/${file.provider}/download?id=${encodeURIComponent(file.id)}&name=${encodeURIComponent(file.name)}`;
      const link = document.createElement("a"); link.href = url; link.click();
    } else if (action === "open" && file.is_folder) openFolder(file);
    else if (action === "rename") {
      const name = prompt("Nowa nazwa:", file.name);
      if (!name) return;
      await api(`/api/files/${file.provider}`, { method: "PATCH", body: JSON.stringify({ id: file.id, name }) });
      toast("Nazwa została zmieniona."); await loadFiles();
    } else if (action === "move") {
      const parent = await chooseMoveTarget(file.provider);
      if (parent === null) return;
      await api(`/api/files/${file.provider}`, { method: "PATCH", body: JSON.stringify({ id: file.id, parent }) });
      toast("Plik został przeniesiony."); await loadFiles();
    } else if (action === "delete") {
      const confirmed = await confirmAction("Usunąć plik?", `Czy na pewno chcesz usunąć „${file.name}”? Tej operacji nie można cofnąć.`);
      if (!confirmed) return;
      await api(`/api/files/${file.provider}`, { method: "DELETE", body: JSON.stringify({ id: file.id }) });
      toast("Plik został usunięty."); await loadFiles();
    }
  } catch (error) { toast(error.message, true); }
}

function chooseMoveTarget(provider) {
  return new Promise((resolve) => {
    const dialog = $("#move-dialog");
    const stack = [{ id: provider === "google" ? "root" : "/", name: provider === "google" ? "Moje pliki" : "MEGA" }];
    let destination = null;
    let active = true;
    const selectHere = () => {
      destination = stack[stack.length - 1].id;
      dialog.close();
    };
    const finish = () => {
      active = false;
      $("#move-select-here").removeEventListener("click", selectHere);
      resolve(destination);
    };
    dialog.addEventListener("close", finish, { once: true });
    $("#move-select-here").addEventListener("click", selectHere);
    const render = async () => {
      if (!active) return;
      $("#move-folder-path").textContent = stack.map((item) => item.name).join(" / ");
      const list = $("#move-folder-list");
      list.innerHTML = `<button class="file-action-option" type="button" data-back="true" ${stack.length === 1 ? "disabled" : ""}>← Folder wyżej</button><p class="move-loading">Ładowanie folderów…</p>`;
      list.querySelector("[data-back]").addEventListener("click", () => {
        if (stack.length > 1) { stack.pop(); render(); }
      });
      try {
        const query = new URLSearchParams({ provider, parent: stack[stack.length - 1].id });
        const data = await api(`/api/files?${query}`);
        if (!active) return;
        const folders = data.files.filter((item) => item.is_folder);
        list.querySelector(".move-loading")?.remove();
        if (!folders.length) list.insertAdjacentHTML("beforeend", '<p class="move-loading">Brak podfolderów.</p>');
        folders.forEach((folder) => {
          const button = document.createElement("button");
          button.className = "file-action-option move-folder-option";
          button.type = "button";
          button.textContent = `▰  ${folder.name}`;
          button.addEventListener("click", () => { stack.push({ id: folder.id, name: folder.name }); render(); });
          list.append(button);
        });
      } catch (error) {
        if (active && list.querySelector(".move-loading")) list.querySelector(".move-loading").textContent = error.message;
      }
    };
    dialog.showModal();
    render();
  });
}

function confirmAction(title, message) {
  return new Promise((resolve) => {
    const dialog = $("#confirm-dialog");
    $("#dialog-title").textContent = title;
    $("#dialog-message").textContent = message;
    const button = $("#dialog-confirm");
    const finish = (event) => {
      dialog.removeEventListener("close", finish);
      resolve(dialog.returnValue === "confirm");
      event?.preventDefault?.();
    };
    dialog.addEventListener("close", finish, { once: true });
    dialog.showModal();
  });
}

function selectedUploadTarget() {
  return state.currentProvider !== "all" ? state.currentProvider : state.provider;
}

function uploadFiles(files) {
  if (!files.length) return;
  const progress = $("#upload-progress");
  progress.classList.remove("hidden");
  let finished = 0;
  const uploadNext = (index) => {
    if (index >= files.length) {
      $("#upload-progress-label").textContent = `Przesłano ${finished} z ${files.length} plików`;
      $("#upload-progress-bar").value = 100;
      setTimeout(() => progress.classList.add("hidden"), 1800);
      loadFiles();
      return;
    }
    const file = files[index];
    const data = new FormData();
    const target = selectedUploadTarget();
    data.append("file", file);
    data.append("provider", target === "all" ? "auto" : target);
    data.append("parent", target === "google" ? state.parents.google : target === "mega" ? state.parents.mega : "root");
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/files");
    xhr.setRequestHeader("X-CSRF-Token", state.csrf);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        const percent = Math.round((event.loaded / event.total) * 100);
        $("#upload-progress-label").textContent = `${file.name} · ${percent}%`;
        $("#upload-progress-bar").value = percent;
      }
    };
    xhr.onload = () => {
      let result = {};
      try { result = JSON.parse(xhr.responseText); } catch { /* handled as a server error below */ }
      if (xhr.status < 200 || xhr.status >= 300) {
        toast(result.error || `Nie udało się przesłać pliku ${file.name}.`, true);
        uploadNext(index + 1);
        return;
      }
      finished += 1;
      uploadNext(index + 1);
    };
    xhr.onerror = () => { toast(`Błąd sieci podczas przesyłania ${file.name}.`, true); uploadNext(index + 1); };
    xhr.send(data);
  };
  uploadNext(0);
}

async function loadNotes() {
  try {
    const query = $("#note-search").value.trim();
    const data = await api(`/api/notes?q=${encodeURIComponent(query)}`);
    state.notes = data.notes;
    renderNotesList();
  } catch (error) { toast(error.message, true); }
}

function renderNotesList() {
  const list = $("#notes-list");
  list.innerHTML = state.notes.map((note) => `<button class="note-list-item ${state.selectedNote?.id === note.id ? "selected" : ""}" data-id="${escapeHtml(note.id)}">
    <span class="note-list-title">${note.pinned ? "★ " : ""}${escapeHtml(note.title || "Bez tytułu")}</span>
    <span class="note-list-preview">${escapeHtml((note.content || "Pusta notatka").replace(/\s+/g, " ").slice(0, 72))}</span>
    <span class="note-list-date">${prettyDate(note.updated_at)}</span>
  </button>`).join("");
  list.querySelectorAll("[data-id]").forEach((button) => button.addEventListener("click", () => selectNote(state.notes.find((note) => note.id === button.dataset.id))));
}

function selectNote(note) {
  state.selectedNote = note;
  $("#note-placeholder").classList.toggle("hidden", Boolean(note));
  $("#note-content").classList.toggle("hidden", !note);
  if (!note) return;
  $("#note-title").value = note.title;
  $("#note-body").value = note.content;
  $("#note-date").textContent = `Utworzono ${prettyDate(note.created_at)} · Zmieniono ${prettyDate(note.updated_at)}`;
  $("#pin-note-button").textContent = note.pinned ? "★" : "☆";
  $("#pin-note-button").classList.toggle("is-favorite", Boolean(note.pinned));
  renderNotesList();
}

async function createNote() {
  try {
    const data = await api("/api/notes", { method: "POST", body: JSON.stringify({ title: "Nowa notatka", content: "" }) });
    await loadNotes();
    selectNote(data.note);
    $("#note-title").focus();
    $("#note-title").select();
  } catch (error) { toast(error.message, true); }
}

function scheduleNoteSave() {
  if (!state.selectedNote) return;
  $("#note-save-status").textContent = "Zapisywanie…";
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(saveNote, 550);
}

async function saveNote() {
  if (!state.selectedNote) return;
  const noteId = state.selectedNote.id;
  const title = $("#note-title").value;
  const content = $("#note-body").value;
  try {
    const data = await api(`/api/notes/${encodeURIComponent(noteId)}`, {
      method: "PATCH",
      body: JSON.stringify({ title, content, pinned: state.selectedNote.pinned }),
    });
    state.selectedNote = data.note;
    await loadNotes();
    renderNotesList();
    if ($("#note-title").value === title && $("#note-body").value === content) {
      $("#note-save-status").textContent = "Zapisano";
      $("#note-date").textContent = `Utworzono ${prettyDate(data.note.created_at)} · Zmieniono ${prettyDate(data.note.updated_at)}`;
    } else {
      scheduleNoteSave();
    }
  } catch (error) { $("#note-save-status").textContent = "Błąd zapisu"; toast(error.message, true); }
}

async function togglePin() {
  if (!state.selectedNote) return;
  try {
    const data = await api(`/api/notes/${encodeURIComponent(state.selectedNote.id)}`, {
      method: "PATCH",
      body: JSON.stringify({ title: $("#note-title").value, content: $("#note-body").value, pinned: !state.selectedNote.pinned }),
    });
    state.selectedNote = data.note;
    await loadNotes();
    selectNote(data.note);
  } catch (error) { toast(error.message, true); }
}

async function deleteNote() {
  if (!state.selectedNote) return;
  if (!await confirmAction("Usunąć notatkę?", "Ta notatka zostanie trwale usunięta.")) return;
  try {
    await api(`/api/notes/${encodeURIComponent(state.selectedNote.id)}`, { method: "DELETE" });
    state.selectedNote = null;
    selectNote(null);
    await loadNotes();
    toast("Notatka została usunięta.");
  } catch (error) { toast(error.message, true); }
}

async function loadSettings() {
  try {
    const [connections, usage] = await Promise.all([api("/api/connections"), api("/api/storage")]);
    $("#google-status").textContent = connections.google ? "Zapisano token OAuth; dostęp jest sprawdzany przy żądaniu." : "Nie połączono. Połącz prywatne konto Google.";
    $("#google-connect").textContent = connections.google ? "Połącz ponownie" : "Połącz Drive";
    $("#google-cloud .connection-state").textContent = connections.google ? "Token zapisany" : "Niepołączono";
    $("#mega-status").textContent = !connections.mega_supported
      ? "MEGA jest wyłączone w konfiguracji serwera."
      : connections.mega ? "Skonfigurowano adres WebDAV MEGAcmd." : "Wymaga lokalnej konfiguracji MEGAcmd na serwerze.";
    $("#mega-cloud .connection-state").textContent = !connections.mega_supported
      ? "Wyłączone" : connections.mega ? "Skonfigurowano" : "Niepołączono";
    const megaFilter = $('.chip[data-provider="mega"]');
    if (megaFilter) {
      megaFilter.disabled = !connections.mega_supported;
      megaFilter.title = connections.mega_supported ? "" : "MEGA jest wyłączone w konfiguracji serwera.";
    }
    const available = Object.entries(usage.storage).filter(([, data]) => data.available);
    const total = available.reduce((sum, [, data]) => sum + (data.total || 0), 0);
    const used = available.reduce((sum, [, data]) => sum + (data.used || 0), 0);
    $("#storage-summary").textContent = total ? `${prettySize(used)} z ${prettySize(total)} zajęte` : "Brak wiarygodnych danych o limicie";
    $("#storage-meter-fill").style.width = total ? `${Math.min(100, (used / total) * 100)}%` : "0%";
    $("#storage-breakdown").innerHTML = available.map(([name, data]) => `<span>${name === "google" ? "Google" : "MEGA"}: ${data.free == null ? "limit nieznany" : `${prettySize(data.free)} wolne`}</span>`).join("");
  } catch (error) { toast(error.message, true); }
}

function toggleTheme() {
  state.theme = state.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = state.theme;
  localStorage.setItem("cloudvault-theme", state.theme);
  $("#theme-toggle").textContent = state.theme === "light" ? "☾" : "☀";
}

async function start() {
  document.documentElement.dataset.theme = state.theme;
  $("#theme-toggle").textContent = state.theme === "light" ? "☾" : "☀";
  try {
    const current = await api("/api/session");
    state.csrf = current.csrf_token;
    if (!current.authenticated) {
      $("#login-screen").classList.remove("hidden");
      $("#app").classList.add("hidden");
      return;
    }
    state.username = current.username;
    $("#profile-username").textContent = current.username;
    $("#login-screen").classList.add("hidden");
    $("#app").classList.remove("hidden");
    await Promise.all([loadFiles(), loadSettings()]);
    if (new URLSearchParams(location.search).get("connected") === "google") {
      history.replaceState({}, "", "/");
      toast("Połączono Google Drive.");
    }
  } catch (error) { toast(error.message, true); }
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#login-error").textContent = "";
  try {
    const result = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ username: $("#username").value, password: $("#password").value }),
    });
    state.csrf = result.csrf_token;
    start();
  } catch (error) { $("#login-error").textContent = error.message; }
});
$("#logout-button").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST", body: "{}" }); location.reload(); }
  catch (error) { toast(error.message, true); }
});
document.querySelectorAll(".nav-item[data-view]").forEach((button) => button.addEventListener("click", () => {
  setView(button.dataset.view);
  if (button.dataset.view === "settings") loadSettings();
}));
document.querySelectorAll(".chip").forEach((button) => button.addEventListener("click", () => {
  state.provider = button.dataset.provider;
  state.currentProvider = "all";
  document.querySelectorAll(".chip").forEach((chip) => chip.classList.toggle("active", chip === button));
  loadFiles();
}));
$("#global-search").addEventListener("input", (event) => { state.query = event.target.value; renderFiles(); });
$("#file-filter").addEventListener("change", renderFiles);
$("#file-sort").addEventListener("change", renderFiles);
document.querySelectorAll(".sort-heading").forEach((button) => button.addEventListener("click", () => {
  $("#file-sort").value = button.dataset.sort;
  renderFiles();
}));
$("#upload-button").addEventListener("click", () => $("#file-input").click());
$("#drop-upload").addEventListener("click", () => $("#file-input").click());
$("#file-input").addEventListener("change", (event) => { uploadFiles([...event.target.files]); event.target.value = ""; });
document.addEventListener("dragover", (event) => {
  if (event.dataTransfer?.types.includes("Files")) {
    event.preventDefault();
    $("#dropzone").classList.add("drag-over");
  }
});
document.addEventListener("dragleave", (event) => {
  if (!event.relatedTarget) $("#dropzone").classList.remove("drag-over");
});
document.addEventListener("drop", (event) => {
  if (event.dataTransfer?.files.length) {
    event.preventDefault();
    $("#dropzone").classList.remove("drag-over");
    uploadFiles([...event.dataTransfer.files]);
  }
});
$("#new-folder-button").addEventListener("click", async () => {
  const name = prompt("Nazwa nowego folderu:");
  if (!name) return;
  const target = selectedUploadTarget() === "all" ? "google" : selectedUploadTarget();
  try {
    await api("/api/folders", { method: "POST", body: JSON.stringify({ name, provider: target, parent: state.parents[target] }) });
    toast("Folder został utworzony."); await loadFiles();
  } catch (error) { toast(error.message, true); }
});
$("#new-note-button").addEventListener("click", createNote);
$("#note-search").addEventListener("input", () => {
  clearTimeout(state.noteSearchTimer);
  state.noteSearchTimer = setTimeout(loadNotes, 200);
});
$("#note-title").addEventListener("input", scheduleNoteSave);
$("#note-body").addEventListener("input", scheduleNoteSave);
$("#pin-note-button").addEventListener("click", togglePin);
$("#delete-note-button").addEventListener("click", deleteNote);
$("#theme-toggle").addEventListener("click", toggleTheme);
$("#settings-theme-toggle").addEventListener("click", toggleTheme);
start();
