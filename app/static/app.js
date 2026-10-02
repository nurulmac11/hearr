"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const view = $("#view");

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

async function api(path, opts = {}) {
  const res = await fetch("/api" + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 401 && !path.startsWith("/auth/")) {
    showLogin();
    throw new Error("Sign in with Plex first.");
  }
  if (!res.ok) throw new Error((data && data.detail) || `Request failed (${res.status})`);
  return data;
}

// ---------------------------------------------------------------------------
// Sign in with Plex

const store = {
  get: (k) => { try { return sessionStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { sessionStorage.setItem(k, v); } catch { /* ignore */ } },
  del: (k) => { try { sessionStorage.removeItem(k); } catch { /* ignore */ } },
};

function showLogin(message = "") {
  document.body.classList.add("locked");
  $("#login").hidden = false;
  $("#login-msg").textContent = message;
  $("#login-btn").disabled = false;
  $("#login-btn").textContent = "Sign in with Plex";
}

async function startLogin() {
  const btn = $("#login-btn");
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Opening Plex…';
  try {
    const r = await api("/auth/start", { method: "POST" });
    store.set("plexPin", String(r.pin_id));
    store.set("returnHash", location.hash || "#/charts");
    location.href = r.auth_url;
  } catch (e) {
    showLogin(e.message);
  }
}

async function finishLogin() {
  const pin = store.get("plexPin");
  const back = store.get("returnHash") || "#/charts";
  history.replaceState(null, "", "/" + back);
  if (!pin) return false;
  $("#login").hidden = false;
  document.body.classList.add("locked");
  $("#login-btn").disabled = true;
  $("#login-btn").innerHTML = '<span class="spin"></span> Signing in…';
  for (let i = 0; i < 20; i++) {
    try {
      const r = await api("/auth/check", { method: "POST", body: JSON.stringify({ pin_id: Number(pin) }) });
      if (r.done) {
        store.del("plexPin");
        return true;
      }
    } catch (e) {
      store.del("plexPin");
      showLogin(e.message);
      return false;
    }
    await new Promise((res) => setTimeout(res, 1500));
  }
  store.del("plexPin");
  showLogin("Plex sign-in didn't finish. Try again.");
  return false;
}

async function logout() {
  await api("/auth/logout", { method: "POST" }).catch(() => {});
  location.reload();
}

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), 5000);
}

const fmtTime = (s) => s ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : "";
const fmtFans = (n) => n == null ? "" : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M fans` : n >= 1e3 ? `${Math.round(n / 1e3)}K fans` : `${n} fans`;
function fmtBytes(b) {
  if (b == null) return "";
  if (b >= 1e9) return `${(b / 1e9).toFixed(b >= 1e10 ? 0 : 1)} GB`;
  const mb = b / 1e6;
  return `${mb >= 100 ? Math.round(mb / 10) * 10 : Math.max(1, Math.round(mb))} MB`;
}
// kbps × seconds → bytes
const sizeFor = (kbps, seconds) => kbps * 1000 / 8 * seconds;

const typeLabel = (t) => ({ album: "Album", ep: "EP", single: "Single", compile: "Compilation" }[t] || "");

// ---------------------------------------------------------------------------
// Request buttons

const STATE_LABEL = {
  library: "✓ In library",
  searching: "Searching…",
  requested: "Requested",
  downloading: "Downloading",
};

// Remember statuses changed in this session so other views of the same album agree.
const statusOverride = new Map();
let currentUser = null;

function statusOf(album) {
  return statusOverride.get(album.id) || album.status || "none";
}

function requestButton(album, cls = "") {
  const st = statusOf(album);
  const attrs = `data-album-state="${album.id}" data-title="${esc(album.title || "")}"`;
  if (st === "wanted") {
    return `<button class="btn ${cls} state-wanted" data-search="${album.id}" ${attrs}
      title="Lidarr wants this but isn't searching right now">⟳ Search now</button>`;
  }
  if (STATE_LABEL[st]) {
    return `<button class="btn ${cls} state-${st}" disabled ${attrs}>${STATE_LABEL[st]}</button>`;
  }
  return `<button class="btn primary ${cls}" data-request="${album.id}" ${attrs}>＋ Request</button>`;
}

function pillFor(album) {
  const st = statusOf(album);
  const label = { library: "IN LIBRARY", wanted: "WANTED", searching: "SEARCHING", requested: "REQUESTED", downloading: "DOWNLOADING" }[st];
  return label ? `<span class="pill ${st}">${label}</span>` : "";
}

function refreshButtons(albumId, btn = null) {
  if (btn && btn.dataset.restore) {
    btn.disabled = false;
    btn.textContent = btn.dataset.restore;
  }
  document.querySelectorAll(`[data-album-state="${albumId}"]`).forEach((old) => {
    const holder = document.createElement("span");
    holder.innerHTML = requestButton({ id: albumId, title: old.dataset.title }, old.classList.contains("sm") ? "sm" : "");
    old.replaceWith(holder.firstElementChild);
  });
}

// Quality chosen for an album, kept while the user picks between candidate releases.
const pendingQuality = new Map();

async function requestAlbum(albumId, foreignAlbumId = null, btn = null, quality = null) {
  if (quality == null) quality = pendingQuality.get(albumId) ?? null;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spin"></span> Requesting';
  }
  try {
    const r = await api("/request/album", {
      method: "POST",
      body: JSON.stringify({ deezer_id: albumId, foreign_album_id: foreignAlbumId, quality_profile_id: quality }),
    });
    if (r.status === "confirm") {
      pendingQuality.set(albumId, quality);
      showCandidates(albumId, r);
      return;
    }
    if (r.status === "not_found") {
      pendingQuality.set(albumId, quality);
      showCandidates(albumId, r, "Not found in Lidarr");
      if (btn) refreshButtons(albumId, btn);
      return;
    }
    pendingQuality.delete(albumId);
    statusOverride.set(albumId, r.status === "library" ? "library" : "requested");
    refreshButtons(albumId);
    toast(r.message, "ok");
    closeModal();
  } catch (e) {
    toast(e.message, "err");
    if (btn) refreshButtons(albumId, btn);
  }
}

async function requestArtist(artistId, btn, quality) {
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Adding';
  try {
    const r = await api("/request/artist", {
      method: "POST", body: JSON.stringify({ deezer_id: artistId, quality_profile_id: quality }),
    });
    toast(r.message, r.status === "not_found" ? "err" : "ok");
    closeModal();
    const main = document.querySelector(`[data-request-artist="${artistId}"]`);
    if (main) {
      main.disabled = true;
      main.textContent = r.status === "not_found" ? "Not found" : "✓ Whole artist requested";
    }
  } catch (e) {
    toast(e.message, "err");
    btn.disabled = false;
    btn.textContent = btn.dataset.restore;
  }
}

function showCandidates(albumId, r, heading = "Which release?") {
  const cands = r.candidates || [];
  openModal(`
    <h2 style="margin-top:0">${esc(heading)}</h2>
    <p class="sub">${esc(r.message)}</p>
    ${r.searched ? `<p class="sub" style="font-size:13px">Searched Lidarr for: ${r.searched.map((t) => `<code>${esc(t)}</code>`).join(", ")}</p>` : ""}
    ${cands.map((c) => `
      <div class="cand">
        <div>
          <div><b>${esc(c.title)}</b></div>
          <div class="m">${esc(c.artist)} · ${esc(c.type || "")}${c.secondary.length ? " · " + esc(c.secondary.join(", ")) : ""}${c.year ? " · " + c.year : ""}${c.tracks ? ` · ${c.tracks} tracks` : ""}${r.status === "not_found" ? ` · match ${Math.round(c.score * 100)}%` : ""}</div>
        </div>
        <button class="btn primary sm" data-pick="${esc(c.foreign_album_id)}" data-album="${albumId}" data-restore="Request this">Request this</button>
      </div>`).join("")}
  `);
}

// ---------------------------------------------------------------------------
// Search now / Remove

function confirmModal(title, body, action) {
  return new Promise((resolve) => {
    openModal(`
      <h2 style="margin-top:0">${title}</h2>
      <p class="sub" style="font-size:15px">${body}</p>
      <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:18px">
        <button class="btn ghost" id="c-no">Cancel</button>
        <button class="btn danger" id="c-yes">${action}</button>
      </div>`);
    $("#c-no").onclick = () => { closeModal(); resolve(null); };
    $("#c-yes").onclick = (e) => resolve(e.currentTarget);
  });
}

async function searchNow(albumId, btn, quality) {
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Searching';
  try {
    const r = await api("/search/album", {
      method: "POST", body: JSON.stringify({ deezer_id: albumId, quality_profile_id: quality }),
    });
    statusOverride.set(albumId, "searching");
    refreshButtons(albumId);
    closeModal();
    toast(r.message, "ok");
  } catch (e) {
    toast(e.message, "err");
    btn.disabled = false;
    btn.textContent = btn.dataset.restore;
  }
}

async function removeAlbum(albumId, title) {
  const yes = await confirmModal(`Remove “${esc(title)}”?`,
    "Deletes its files from your music library, stops Lidarr from downloading it again and deletes the " +
    "seeding torrent. If it was the artist's only album, the artist is removed from Lidarr too.", "Remove");
  if (!yes) return;
  yes.disabled = true;
  yes.innerHTML = '<span class="spin"></span> Removing';
  try {
    const r = await api("/remove/album", { method: "POST", body: JSON.stringify({ deezer_id: albumId }) });
    statusOverride.set(albumId, "none");
    closeModal();
    toast(r.message, "ok");
    route();
  } catch (e) {
    closeModal();
    toast(e.message, "err");
  }
}

async function removeArtist(artistId, name) {
  const yes = await confirmModal(`Remove ${esc(name)}?`,
    `Deletes <b>every</b> file by ${esc(name)} from your music library, removes them from Lidarr and ` +
    "deletes their seeding torrents. This can't be undone.", "Remove artist");
  if (!yes) return;
  yes.disabled = true;
  yes.innerHTML = '<span class="spin"></span> Removing';
  try {
    const r = await api("/remove/artist", { method: "POST", body: JSON.stringify({ deezer_id: artistId }) });
    statusOverride.clear();
    closeModal();
    toast(r.message, "ok");
    route();
  } catch (e) {
    closeModal();
    toast(e.message, "err");
  }
}

// ---------------------------------------------------------------------------
// Quality picker (shown before every request)

let qualityCache = null;

function savedQuality() {
  try { return Number(localStorage.getItem("hearrQuality")) || null; } catch { return null; }
}

async function openQualityPicker(kind, id, title) {
  openModal('<div class="loading">Loading…</div>');
  let q, ctx;
  try {
    [q, ctx] = await Promise.all([
      qualityCache || api("/qualities"),
      api(`/request/context?${kind === "artist" ? "artist_id" : "album_id"}=${id}`),
    ]);
    qualityCache = q;
  } catch (e) {
    closeModal();
    toast(e.message, "err");
    return;
  }
  const ids = q.profiles.map((p) => p.id);
  const current = ctx.quality_profile_id;
  let selected = [current, savedQuality(), q.default].find((x) => x && ids.includes(x));
  const nameOf = (pid) => q.profiles.find((p) => p.id === pid)?.name || "";
  const heading = kind === "album" ? `Request “${esc(title)}”`
    : kind === "search" ? `Search for “${esc(title)}”` : `Add all of ${esc(ctx.artist)}`;
  const action = { album: "Request", search: "Search now", artist: "Add artist" }[kind];

  const est = (p) => ({
    typical: sizeFor(p.kbps.typical, ctx.seconds),
    high: sizeFor(p.kbps.high, ctx.seconds),
    low: sizeFor(p.kbps.low, ctx.seconds),
  });
  const sizeLabel = (p) => {
    if (!ctx.seconds) return "";
    const e = est(p);
    const more = e.high > e.typical * 1.3 ? `<div class="m">up to ${fmtBytes(e.high)} if hi-res</div>`
      : e.low < e.typical * 0.7 ? `<div class="m">${fmtBytes(e.low)}–${fmtBytes(e.typical)}</div>` : "";
    return `<div class="qsize"><b>${ctx.rough ? "~" : "≈ "}${fmtBytes(e.typical)}</b>${more}</div>`;
  };
  const sizeNote = () => {
    if (!ctx.seconds) return "";
    const mins = Math.round(ctx.seconds / 60);
    const what = kind === "artist"
      ? `${ctx.albums} release${ctx.albums === 1 ? "" : "s"} not in your library yet, about ${mins >= 120 ? Math.round(mins / 60) + " hours" : mins + " min"} of music${ctx.rough ? " (rough: Lidarr may pick a slightly different set)" : ""}`
      : `${mins} min of music`;
    const p = q.profiles.find((x) => x.id === selected);
    const need = p ? est(p).typical : 0;
    const free = ctx.free_bytes;
    const tight = free != null && need > free - 5e9;
    return `<p class="qnote">${what}.${free != null ? ` <span class="${tight ? "qbad" : ""}">${fmtBytes(free)} free on the music disk${tight ? ", which may not be enough" : ""}.</span>` : ""}</p>`;
  };

  const render = () => {
    const warn = ctx.in_library && current && selected !== current
      ? `<p class="qwarn">${esc(ctx.artist)} is already in Lidarr at <b>${esc(nameOf(current))}</b>. Switching to
         <b>${esc(nameOf(selected))}</b> changes the quality for all of their albums.</p>`
      : ctx.in_library ? `<p class="sub">${esc(ctx.artist)} is already in Lidarr at <b>${esc(nameOf(current))}</b>.</p>` : "";
    $("#modal-body").innerHTML = `
      <h2 style="margin-top:0">${heading}</h2>
      ${kind === "artist" ? `<p class="sub">Every album will be downloaded.</p>` : ""}
      <div class="qlist">${q.profiles.map((p) => `
        <label class="qopt ${p.id === selected ? "on" : ""}">
          <input type="radio" name="quality" value="${p.id}" ${p.id === selected ? "checked" : ""}>
          <div style="flex:1;min-width:0"><b>${esc(p.name)}</b><div class="m">${esc(p.description)}</div></div>
          ${sizeLabel(p)}
        </label>`).join("")}</div>
      ${sizeNote()}
      ${warn}
      <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:18px">
        <button class="btn ghost" id="q-cancel">Cancel</button>
        <button class="btn primary" id="q-go" data-restore="${action}">${action}</button>
      </div>`;
    $("#modal-body").querySelectorAll('input[name="quality"]').forEach((r) => r.onchange = () => {
      selected = Number(r.value);
      render();
    });
    $("#q-cancel").onclick = closeModal;
    $("#q-go").onclick = (ev) => {
      try { localStorage.setItem("hearrQuality", String(selected)); } catch { /* ignore */ }
      if (kind === "album") requestAlbum(id, null, ev.currentTarget, selected);
      else if (kind === "search") searchNow(id, ev.currentTarget, selected);
      else requestArtist(id, ev.currentTarget, selected);
    };
  };
  render();
}

document.addEventListener("click", (e) => {
  const req = e.target.closest("[data-request]");
  if (req) {
    e.preventDefault();
    e.stopPropagation();
    openQualityPicker("album", Number(req.dataset.request), req.dataset.title || "");
    return;
  }
  const srch = e.target.closest("[data-search]");
  if (srch) {
    e.preventDefault();
    e.stopPropagation();
    openQualityPicker("search", Number(srch.dataset.search), srch.dataset.title || "");
    return;
  }
  const rmAlbum = e.target.closest("[data-remove-album]");
  if (rmAlbum) {
    removeAlbum(Number(rmAlbum.dataset.removeAlbum), rmAlbum.dataset.title);
    return;
  }
  const rmArtist = e.target.closest("[data-remove-artist]");
  if (rmArtist) {
    removeArtist(Number(rmArtist.dataset.removeArtist), rmArtist.dataset.name);
    return;
  }
  const pick = e.target.closest("[data-pick]");
  if (pick) {
    requestAlbum(Number(pick.dataset.album), pick.dataset.pick, pick);
    return;
  }
  const art = e.target.closest("[data-request-artist]");
  if (art) {
    openQualityPicker("artist", Number(art.dataset.requestArtist), art.dataset.name);
    return;
  }
  const play = e.target.closest("[data-preview]");
  if (play) {
    togglePreview(play);
  }
});

// ---------------------------------------------------------------------------
// Modal

function openModal(html) {
  $("#modal-body").innerHTML = html;
  $("#modal").hidden = false;
}
function closeModal() { $("#modal").hidden = true; }
$("#modal").addEventListener("click", (e) => {
  if (e.target.id === "modal" || e.target.closest(".modal-close")) closeModal();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

// ---------------------------------------------------------------------------
// 30-second previews

const audio = new Audio();
let playingBtn = null;

function togglePreview(btn) {
  if (playingBtn === btn && !audio.paused) {
    audio.pause();
    return;
  }
  audio.src = btn.dataset.preview;
  audio.play().catch(() => toast("Preview not available", "err"));
  setPlaying(btn);
  const p = $("#player");
  p.hidden = false;
  p.innerHTML = `
    ${btn.dataset.cover ? `<img src="${esc(btn.dataset.cover)}" alt="">` : ""}
    <div class="grow"><div class="pt">${esc(btn.dataset.title)}</div><div class="pa">${esc(btn.dataset.artist)} · preview</div>
    <progress max="1" value="0"></progress></div>
    <button class="btn sm ghost" id="player-stop">Stop</button>`;
  $("#player-stop").onclick = () => { audio.pause(); p.hidden = true; };
}

function setPlaying(btn) {
  if (playingBtn) { playingBtn.classList.remove("on"); playingBtn.textContent = "▶"; }
  playingBtn = btn;
  if (btn) { btn.classList.add("on"); btn.textContent = "❚❚"; }
}
audio.addEventListener("pause", () => setPlaying(null));
audio.addEventListener("ended", () => { setPlaying(null); $("#player").hidden = true; });
audio.addEventListener("timeupdate", () => {
  const bar = $("#player progress");
  if (bar && audio.duration) bar.value = audio.currentTime / audio.duration;
});

// ---------------------------------------------------------------------------
// Building blocks

function albumCard(a, { showArtist = true, showType = false } = {}) {
  const sub = [
    showArtist && a.artist_id ? `<a href="#/artist/${a.artist_id}">${esc(a.artist)}</a>` : (showArtist ? esc(a.artist) : ""),
    showType ? typeLabel(a.type) : "",
    a.year || "",
  ].filter(Boolean).join(" · ");
  return `
    <div class="card">
      <a class="cover" href="#/album/${a.id}">
        ${a.cover ? `<img src="${esc(a.cover)}" alt="" loading="lazy">` : ""}
        ${pillFor(a)}
        <span class="overlay">${statusOf(a) === "library" ? "" : requestButton(a, "sm")}</span>
      </a>
      <div class="t" title="${esc(a.title)}">${esc(a.title)}</div>
      <div class="s">${sub}</div>
    </div>`;
}

function artistCard(a) {
  return `
    <a class="card artist-card" href="#/artist/${a.id}">
      <div class="cover">${a.picture ? `<img src="${esc(a.picture)}" alt="" loading="lazy">` : ""}</div>
      <div class="t">${esc(a.name)}</div>
      ${a.in_library ? '<div class="badge">✓ In your library</div>' : a.because ? `<div class="because">Like ${esc(a.because.join(", "))}</div>` : `<div class="s">${fmtFans(a.fans)}</div>`}
    </a>`;
}

function trackRow(t, i) {
  const al = t.album;
  return `
    <div class="track">
      <div class="n">${i + 1}</div>
      ${al && al.cover ? `<img src="${esc(al.cover)}" alt="" loading="lazy">` : "<span></span>"}
      <div style="min-width:0">
        <div class="tt">${esc(t.title)}</div>
        <div class="a">${t.artist_id ? `<a href="#/artist/${t.artist_id}">${esc(t.artist)}</a>` : esc(t.artist)}</div>
      </div>
      <div class="al">${al ? `<a href="#/album/${al.id}">${esc(al.title)}</a>` : ""}</div>
      <div style="display:flex;gap:8px;align-items:center">
        ${t.preview ? `<button class="play" title="Play preview" data-preview="${esc(t.preview)}" data-title="${esc(t.title)}" data-artist="${esc(t.artist)}" data-cover="${esc(al?.cover || "")}">▶</button>` : ""}
        ${al ? requestButton(al, "sm") : ""}
      </div>
    </div>`;
}

const tracksList = (tracks) => `<div class="tracks">${tracks.map(trackRow).join("")}</div>`;
const albumGrid = (albums, opts) => albums.length ? `<div class="grid">${albums.map((a) => albumCard(a, opts)).join("")}</div>` : '<div class="empty">Nothing here.</div>';
const artistRow = (artists) => `<div class="row-scroll">${artists.map(artistCard).join("")}</div>`;
const skeletonGrid = (n = 12) => `<div class="grid">${Array.from({ length: n }, () => '<div><div class="cover skeleton"></div></div>').join("")}</div>`;

// ---------------------------------------------------------------------------
// Views

let chartList = null;

async function viewCharts(key) {
  chartList = chartList || await api("/charts");
  if (!key || !chartList.some((c) => c.key === key)) {
    location.replace(`#/charts/${chartList[0].key}`);
    return;
  }
  const tabs = `
    <h1>Top charts</h1>
    <div class="tabs">
      ${chartList.map((c) => `<a class="tab ${key === c.key ? "active" : ""}" href="#/charts/${esc(c.key)}">${esc(c.name)}</a>`).join("")}
      <a class="tab" href="#/genres">By genre</a>
    </div>`;
  view.innerHTML = tabs + skeletonGrid();
  const d = await api(`/charts/${key}`);
  let tab = sessionStorage.getItem("chartTab") || "albums";
  const render = () => {
    view.innerHTML = tabs + `
      <h2>Top artists</h2>${artistRow(d.artists)}
      <div class="tabs" style="margin-top:28px">
        <button class="tab ${tab === "albums" ? "active" : ""}" data-t="albums">Albums & singles</button>
        <button class="tab ${tab === "tracks" ? "active" : ""}" data-t="tracks">Top 100 songs</button>
      </div>
      <div style="margin-top:16px">${tab === "albums" ? albumGrid(d.albums) : tracksList(d.tracks)}</div>`;
    view.querySelectorAll("[data-t]").forEach((b) => b.onclick = () => {
      tab = b.dataset.t;
      try { sessionStorage.setItem("chartTab", tab); } catch { /* ignore */ }
      render();
    });
  };
  render();
}

async function viewGenres() {
  view.innerHTML = "<h1>Genres</h1>" + '<div class="loading">Loading…</div>';
  const genres = await api("/genres");
  view.innerHTML = `<h1>Genres</h1><p class="sub">Top albums, artists and songs per genre.</p>
    <div class="genre-grid">${genres.map((g) => `
      <a class="genre" href="#/genre/${g.id}/${encodeURIComponent(g.name)}">
        ${g.picture ? `<img src="${esc(g.picture)}" alt="" loading="lazy">` : ""}<span>${esc(g.name)}</span>
      </a>`).join("")}</div>`;
}

async function viewGenre(id, name) {
  const title = `<h1>${esc(name || "Genre")}</h1><div class="tabs"><a class="tab" href="#/genres">← All genres</a></div>`;
  view.innerHTML = title + skeletonGrid();
  const d = await api(`/genre/${id}`);
  view.innerHTML = title + `
    <h2>Top albums</h2>${albumGrid(d.albums)}
    <h2>Top artists</h2>${artistRow(d.artists)}
    <h2>Top songs</h2>${tracksList(d.tracks)}`;
}

async function viewNew() {
  view.innerHTML = "<h1>New releases</h1>" + skeletonGrid();
  const d = await api("/new");
  view.innerHTML = `<h1>New releases</h1>
    <h2>From artists in your library <small>last ${d.days} days</small></h2>
    ${d.yours.length ? albumGrid(d.yours, { showType: true }) : '<div class="empty">No recent releases from your artists yet.</div>'}
    <h2>Deezer picks</h2>${albumGrid(d.picks)}`;
}

async function viewDiscover() {
  view.innerHTML = "<h1>Discover</h1><p class='sub'>Finding artists similar to your library…</p>" + skeletonGrid(8);
  const d = await api("/discover");
  if (!d.seeds.length) {
    view.innerHTML = `<h1>Discover</h1><div class="empty">Add a few artists to Lidarr first. Recommendations are based on your library.<br><br><a class="btn primary" href="#/charts">Browse the charts</a></div>`;
    return;
  }
  view.innerHTML = `<h1>Discover</h1>
    <p class="sub">Based on ${d.seeds.length} artist${d.seeds.length > 1 ? "s" : ""} in your library.</p>
    <h2>Recommended for you</h2>
    <div class="grid">${d.recommended.map(artistCard).join("")}</div>
    ${d.rows.filter((r) => r.artists.length).map((r) => `
      <h2>Because you have <a href="#/artist/${r.seed.id}" style="text-decoration:underline">${esc(r.seed.name)}</a></h2>
      ${artistRow(r.artists)}`).join("")}`;
}

async function viewSearch(q) {
  $("#search").value = q;
  if (!q) { view.innerHTML = '<div class="empty">Type to search.</div>'; return; }
  view.innerHTML = `<h1>Results for “${esc(q)}”</h1>` + skeletonGrid(6);
  const d = await api(`/search?q=${encodeURIComponent(q)}`);
  view.innerHTML = `<h1>Results for “${esc(q)}”</h1>
    ${d.artists.length ? `<h2>Artists</h2>${artistRow(d.artists)}` : ""}
    ${d.albums.length ? `<h2>Albums</h2>${albumGrid(d.albums, { showType: true })}` : ""}
    ${d.tracks.length ? `<h2>Songs</h2>${tracksList(d.tracks)}` : ""}
    ${!d.artists.length && !d.albums.length && !d.tracks.length ? '<div class="empty">No results.</div>' : ""}`;
}

async function viewArtist(id) {
  view.innerHTML = '<div class="loading">Loading…</div>';
  const d = await api(`/artist/${id}`);
  const a = d.artist;
  const groups = [["album", "Albums"], ["ep", "EPs"], ["single", "Singles"], ["compile", "Compilations"]];
  let filter = "album";
  const counts = Object.fromEntries(groups.map(([k]) => [k, d.albums.filter((x) => x.type === k).length]));
  if (!counts.album) filter = groups.find(([k]) => counts[k])?.[0] || "album";

  const render = () => {
    view.innerHTML = `
      <div class="hero round">
        ${a.picture ? `<img src="${esc(a.picture)}" alt="">` : ""}
        <div class="meta">
          <div class="kicker">Artist${a.in_library ? " · ✓ in your library" : ""}</div>
          <h1>${esc(a.name)}</h1>
          <div class="sub">${fmtFans(a.fans)}</div>
          <div class="actions">
            <button class="btn ghost" data-request-artist="${a.id}" data-name="${esc(a.name)}">Add whole artist</button>
            ${currentUser?.owner && a.in_library ? `<button class="btn ghost danger-text" data-remove-artist="${a.id}" data-name="${esc(a.name)}">Remove artist</button>` : ""}
          </div>
        </div>
      </div>
      ${d.top.length ? `<h2>Popular</h2>${tracksList(d.top)}` : ""}
      <h2>Discography</h2>
      <div class="chips">${groups.filter(([k]) => counts[k]).map(([k, label]) =>
        `<button class="chip ${filter === k ? "active" : ""}" data-f="${k}" style="border:0">${label} ${counts[k]}</button>`).join("")}</div>
      <div style="margin-top:16px">${albumGrid(d.albums.filter((x) => x.type === filter), { showArtist: false })}</div>
      ${d.related.length ? `<h2>Fans also like</h2>${artistRow(d.related)}` : ""}`;
    view.querySelectorAll("[data-f]").forEach((b) => b.onclick = () => { filter = b.dataset.f; render(); });
  };
  render();
}

async function viewAlbum(id) {
  view.innerHTML = '<div class="loading">Loading…</div>';
  const d = await api(`/album/${id}`);
  const a = d.album;
  view.innerHTML = `
    <div class="hero">
      ${a.cover ? `<img src="${esc(a.cover)}" alt="">` : ""}
      <div class="meta">
        <div class="kicker">${typeLabel(a.type) || "Album"}${a.year ? " · " + a.year : ""}</div>
        <h1>${esc(a.title)}</h1>
        <div class="sub"><a href="#/artist/${a.artist_id}" style="text-decoration:underline">${esc(a.artist)}</a>
          · ${d.tracks.length} tracks${a.label ? " · " + esc(a.label) : ""}</div>
        ${a.genres.length ? `<div class="chips">${a.genres.map((g) => `<span class="chip">${esc(g)}</span>`).join("")}</div>` : ""}
        <div class="actions">${requestButton(a)}
          ${currentUser?.owner && statusOf(a) !== "none" ? `<button class="btn ghost danger-text" data-remove-album="${a.id}" data-title="${esc(a.title)}">Remove</button>` : ""}</div>
      </div>
    </div>
    <h2>Tracks</h2>
    <div class="tracks">${d.tracks.map((t, i) => `
      <div class="track" style="grid-template-columns:28px 1fr auto auto">
        <div class="n">${i + 1}</div>
        <div style="min-width:0"><div class="tt">${esc(t.title)}</div>
          ${t.artist && t.artist !== a.artist ? `<div class="a">${esc(t.artist)}</div>` : ""}</div>
        <div class="a">${fmtTime(t.duration)}</div>
        ${t.preview ? `<button class="play" data-preview="${esc(t.preview)}" data-title="${esc(t.title)}" data-artist="${esc(a.artist)}" data-cover="${esc(a.cover)}">▶</button>` : "<span></span>"}
      </div>`).join("")}</div>`;
}

async function viewRequests() {
  view.innerHTML = "<h1>Requests</h1><div class='loading'>Loading…</div>";
  const d = await api("/requests");
  const label = {
    library: ["✓ Downloaded", "state-library"], wanted: ["Wanted", "state-wanted"],
    searching: ["Searching…", "state-wanted"],
    downloading: ["Downloading", "state-downloading"], refreshing: ["Adding to Lidarr", "state-requested"],
    requested: ["Requested", "state-requested"], failed: ["Failed", ""], missing: ["Removed from Lidarr", ""],
    known: ["Not monitored", ""], artist: ["Whole artist", "state-requested"], unknown: ["Lidarr offline", ""],
  };
  view.innerHTML = `<h1>Requests</h1>
    <p class="sub">Everything requested from Hearr. <a href="${esc(d.lidarr_url)}" target="_blank" rel="noopener" style="text-decoration:underline">Open Lidarr</a></p>
    ${d.requests.length ? d.requests.map((r) => {
      const [text, cls] = label[r.status] || [r.status, ""];
      const when = new Date(r.created_at * 1000).toLocaleString();
      const link = r.kind === "album" ? `#/album/${r.deezer_id}` : `#/artist/${r.deezer_id}`;
      return `
      <div class="req">
        <a href="${link}">${r.cover ? `<img src="${esc(r.cover)}" alt="">` : "<span></span>"}</a>
        <div style="min-width:0">
          <div class="kind">${r.kind === "album" ? "Album" : "Artist"} · ${esc(when)}${r.requested_by ? ` · ${esc(r.requested_by)}` : ""}</div>
          <div><b><a href="${link}">${esc(r.title)}</a></b>${r.kind === "album" ? ` <span class="sub">by ${esc(r.artist)}</span>` : ""}</div>
          ${r.message ? `<div class="sub">${esc(r.message)}</div>` : ""}
          ${r.progress != null && r.status !== "library" ? `<div class="bar"><i style="width:${Math.min(100, r.progress)}%"></i></div>` : ""}
        </div>
        <span class="btn sm ${cls}">${esc(text)}</span>
      </div>`;
    }).join("") : '<div class="empty">No requests yet. Find something in Charts or Discover.</div>'}`;
}

// ---------------------------------------------------------------------------
// Router

const routes = [
  [/^#\/charts(?:\/([\w-]+))?$/, (m) => viewCharts(m[1]), "charts"],
  [/^#\/genres$/, () => viewGenres(), "genres"],
  [/^#\/genre\/(\d+)(?:\/(.*))?$/, (m) => viewGenre(m[1], decodeURIComponent(m[2] || "")), "genres"],
  [/^#\/new$/, () => viewNew(), "new"],
  [/^#\/discover$/, () => viewDiscover(), "discover"],
  [/^#\/search\/(.*)$/, (m) => viewSearch(decodeURIComponent(m[1])), ""],
  [/^#\/artist\/(\d+)$/, (m) => viewArtist(m[1]), ""],
  [/^#\/album\/(\d+)$/, (m) => viewAlbum(m[1]), ""],
  [/^#\/requests$/, () => viewRequests(), "requests"],
];

async function route() {
  const hash = location.hash || "#/charts";
  closeModal();
  for (const [re, fn, nav] of routes) {
    const m = hash.match(re);
    if (m) {
      document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === nav));
      window.scrollTo(0, 0);
      try {
        await fn(m);
      } catch (e) {
        view.innerHTML = `<div class="error">${esc(e.message)}</div>`;
      }
      return;
    }
  }
  location.hash = "#/charts";
}

window.addEventListener("hashchange", route);

$("#search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#search").value.trim();
  if (q) location.hash = `#/search/${encodeURIComponent(q)}`;
});

async function loadStatus(user) {
  const el = $("#lidarr-status");
  const who = user ? `<div class="me">${user.thumb ? `<img src="${esc(user.thumb)}" alt="">` : ""}<span>${esc(user.username)}</span>
    <button class="linkish" id="logout">Sign out</button></div>` : "";
  try {
    const s = await api("/status");
    el.innerHTML = who + (s.lidarr
      ? `<span class="dot" style="background:var(--ok)"></span>Lidarr ${esc(s.version)}`
      : `<span class="dot" style="background:var(--bad)"></span>Lidarr offline`);
  } catch {
    el.innerHTML = who + `<span class="dot" style="background:var(--bad)"></span>Server offline`;
  }
  const out = $("#logout");
  if (out) out.onclick = logout;
}

$("#login-btn").addEventListener("click", startLogin);

(async function boot() {
  if (new URLSearchParams(location.search).has("plexauth")) {
    await finishLogin();
  }
  let user = null;
  try {
    user = await api("/auth/me");
  } catch {
    showLogin();
    return;
  }
  currentUser = user;
  document.body.classList.remove("locked");
  $("#login").hidden = true;
  loadStatus(user);
  route();
})();
