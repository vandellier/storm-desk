(() => {
  "use strict";

  const STORE_KEY = "stormdesk.filters.v1";
  const state = {
    alerts: [],
    frames: [],
    tracks: { type: "FeatureCollection", features: [] },
    rotation: [],
    site: null,
    layer: "reflectivity",
    playing: true,
    frameIndex: 0,
    opacity: 0.72,
    kinds: new Set(["tornado", "severe", "flood", "winter", "tropical", "fire", "other"]),
    filters: { states: [], counties: [], cities: [] },
    knownIds: new Set(),
    notify: false,
    geo: { states: [], counties: [], cities: [] },
    tickerSig: "",
    tickerHold: "",
    frameSig: "",
    trackSig: "",
    rotSig: "",
    readerOpen: false,
    readerId: "",
    readerSig: "",
    placePinned: false,
    watchKey: "",
  };

  const el = (id) => document.getElementById(id);
  const map = L.map("map", { zoomControl: true, worldCopyJump: false }).setView([39.5, -98.35], 5);

  map.createPane("sweepPane");
  map.getPane("sweepPane").style.zIndex = 450;
  map.getPane("sweepPane").style.pointerEvents = "none";

  const alertLayer = L.geoJSON(null, { style: alertStyle, onEachFeature: bindAlert }).addTo(map);
  const trackLayer = L.layerGroup().addTo(map);
  const rotLayer = L.layerGroup().addTo(map);
  const radioLayer = L.layerGroup().addTo(map);
  let radarLayers = [];
  let velocityLayer = null;
  let nstLayer = null;
  let loopTimer = null;
  let fadeTimer = null;
  let holdTicks = 0;
  let sweepEl = null;

  bootstrap();

  async function bootstrap() {
    loadFilters();
    await Promise.all([loadStates(), loadStatus(), loadBasemap()]);
    renderChips();
    wireUi();
    connectWs();
    startLoop();
    map.on("move zoom zoomend viewreset", updateSweep);
    map.on("moveend", debounce(onMapMoved, 600));
    window.addEventListener("resize", debounce(() => {
      map.invalidateSize();
      fitPlayer();
    }, 150));
    focusSavedPlace(false);
    setInterval(() => refreshRest(), 120000);
    setInterval(refreshCameraStills, 60000);
  }

  async function loadStates() {
    const data = await (await fetch("/api/geo/states")).json();
    state.geo.states = data.states || [];
    const sel = el("state-select");
    for (const s of state.geo.states) {
      const opt = document.createElement("option");
      opt.value = s.abbr;
      opt.textContent = `${s.name} (${s.abbr})`;
      sel.appendChild(opt);
    }
    if (state.filters.states[0]) sel.value = state.filters.states[0];
  }

  async function loadStatus() {
    try {
      const s = await (await fetch("/api/status")).json();
      el("status-nws").textContent = `NWS ${s.alerts.count} alerts`;
    } catch (_) {
      /* first paint */
    }
  }

  async function loadBasemap() {
    let cartoKey = "";
    try {
      const cfg = await (await fetch("/api/config")).json();
      cartoKey = (cfg.cartoKey || "").trim();
    } catch (_) {
      /* use Esri */
    }
    if (cartoKey) {
      L.tileLayer(`https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png?key=${encodeURIComponent(cartoKey)}`, {
        attribution: "&copy; OSM &copy; CARTO · NWS · RainViewer · IEM · NOAA NEXRAD",
        subdomains: "abcd",
        maxZoom: 16,
      }).addTo(map);
      return;
    }
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}", {
      attribution: "Tiles © Esri · NWS · RainViewer · IEM · NOAA NEXRAD",
      maxZoom: 16,
    }).addTo(map);
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}", {
      maxZoom: 16,
    }).addTo(map);
  }

  function wireUi() {
    el("state-select").addEventListener("change", () => {
      const v = el("state-select").value;
      state.filters.states = v ? [v] : [];
      persist();
      const st = state.geo.states.find((s) => s.abbr === v);
      if (st) {
        watchPlace({ lat: st.lat, lon: st.lon, state: st.abbr, fips: "", name: st.name, same: "" }, true);
        map.flyTo([st.lat, st.lon], 6, { duration: 0.8 });
        revealLocal();
      } else {
        state.placePinned = false;
        state.watchKey = "";
        onMapMoved();
      }
      renderAlerts();
    });
    setupSuggest("county-q", "county-results", searchCounties, addCounty);
    setupSuggest("city-q", "city-results", searchCities, addCity);
    el("clear-filters").addEventListener("click", () => {
      state.filters = { states: [], counties: [], cities: [] };
      el("state-select").value = "";
      state.placePinned = false;
      state.watchKey = "";
      persist();
      renderChips();
      renderAlerts();
      onMapMoved();
    });
    el("use-location").addEventListener("click", useLocation);
    document.querySelectorAll("[data-kind]").forEach((box) => {
      box.addEventListener("change", () => {
        if (box.checked) state.kinds.add(box.dataset.kind);
        else state.kinds.delete(box.dataset.kind);
        renderAlerts();
      });
    });
    document.querySelectorAll("[data-layer]").forEach((btn) => {
      btn.addEventListener("click", () => setLayer(btn.dataset.layer));
    });
    el("loop-toggle").addEventListener("click", () => {
      state.playing = !state.playing;
      el("loop-toggle").textContent = state.playing ? "❚❚" : "▶";
    });
    el("loop-slider").addEventListener("input", (e) => {
      state.playing = false;
      el("loop-toggle").textContent = "▶";
      showFrame(Number(e.target.value));
    });
    el("radar-opacity").addEventListener("input", (e) => {
      state.opacity = Number(e.target.value) / 100;
      applyOpacity();
    });
    el("reader-close").addEventListener("click", closeReader);
    el("shot-close").addEventListener("click", closeShot);
    el("nwr-close").addEventListener("click", stopNwr);
    el("nwr-audio").addEventListener("error", onNwrError);
    el("nwr-audio").addEventListener("playing", () => {
      el("nwr-status").textContent = "Live volunteer stream. It can lag behind the radio by up to a minute.";
      fitPlayer();
    });
    el("ticker-live").addEventListener("click", () => {
      el("ticker").classList.toggle("is-held");
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (!el("shot").hidden) closeShot();
      else if (state.readerOpen) closeReader();
      else if (!el("nwr-player").hidden) stopNwr();
      else el("sidebar").classList.remove("open");
    });
    el("notify-btn").addEventListener("click", enableNotify);
    el("sidebar-open").addEventListener("click", () => el("sidebar").classList.add("open"));
    el("sidebar-close").addEventListener("click", () => el("sidebar").classList.remove("open"));
  }

  function setupSuggest(inputId, boxId, searchFn, pickFn) {
    const input = el(inputId);
    const box = el(boxId);
    let t = null;
    input.addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(async () => {
        const q = input.value.trim();
        if (q.length < 1) {
          box.hidden = true;
          return;
        }
        const items = await searchFn(q);
        box.innerHTML = "";
        items.forEach((item) => {
          const b = document.createElement("button");
          b.type = "button";
          b.innerHTML = `${escapeHtml(item.label)} <small>${escapeHtml(item.sub)}</small>`;
          b.addEventListener("click", () => {
            pickFn(item.raw);
            input.value = "";
            box.hidden = true;
          });
          box.appendChild(b);
        });
        box.hidden = items.length === 0;
      }, 180);
    });
    document.addEventListener("click", (e) => {
      if (!box.contains(e.target) && e.target !== input) box.hidden = true;
    });
  }

  async function searchCounties(q) {
    const st = state.filters.states[0] || "";
    const data = await (await fetch(`/api/geo/counties?state=${st}&q=${encodeURIComponent(q)}&limit=30`)).json();
    return (data.counties || []).map((c) => ({
      label: c.n,
      sub: `${c.s} · ${c.u}`,
      raw: c,
    }));
  }

  async function searchCities(q) {
    const st = state.filters.states[0] || "";
    const data = await (await fetch(`/api/geo/cities?state=${st}&q=${encodeURIComponent(q)}&limit=20`)).json();
    return (data.cities || []).map((c) => ({
      label: c.n,
      sub: `${c.s} · ${c.c || "city"}`,
      raw: c,
    }));
  }

  function addCounty(c) {
    if (!state.filters.counties.some((x) => x.u === c.u)) state.filters.counties.push(c);
    if (!state.filters.states.includes(c.s)) {
      state.filters.states = [c.s];
      el("state-select").value = c.s;
    }
    persist();
    renderChips();
    renderAlerts();
    watchPlace(
      { lat: c.lat, lon: c.lon, state: c.s, fips: c.f || "", name: `${c.n} County, ${c.s}`, same: "" },
      true
    );
    map.flyTo([c.lat, c.lon], 8, { duration: 0.7 });
    revealLocal();
  }

  function addCity(c) {
    if (!state.filters.cities.some((x) => x.n === c.n && x.s === c.s)) state.filters.cities.push(c);
    if (!state.filters.states.includes(c.s)) {
      state.filters.states = [c.s];
      el("state-select").value = c.s;
    }
    persist();
    renderChips();
    renderAlerts();
    watchPlace(
      { lat: c.lat, lon: c.lon, state: c.s, fips: c.f || "", name: `${c.n}, ${c.s}`, same: "" },
      true
    );
    map.flyTo([c.lat, c.lon], 9, { duration: 0.7 });
    revealLocal();
  }

  function renderChips() {
    const box = el("chips");
    box.innerHTML = "";
    const add = (label, onRemove) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.innerHTML = `${escapeHtml(label)} <button type="button" aria-label="Remove">×</button>`;
      chip.querySelector("button").addEventListener("click", onRemove);
      box.appendChild(chip);
    };
    state.filters.counties.forEach((c, i) =>
      add(`${c.n} Co, ${c.s}`, () => {
        state.filters.counties.splice(i, 1);
        persist();
        renderChips();
        renderAlerts();
        focusSavedPlace(true);
      })
    );
    state.filters.cities.forEach((c, i) =>
      add(`${c.n}, ${c.s}`, () => {
        state.filters.cities.splice(i, 1);
        persist();
        renderChips();
        renderAlerts();
        focusSavedPlace(true);
      })
    );
  }

  function focusSavedPlace(animate) {
    const city = state.filters.cities[state.filters.cities.length - 1];
    const county = state.filters.counties[state.filters.counties.length - 1];
    const abbr = state.filters.states[0];
    const st = (state.geo.states || []).find((s) => s.abbr === abbr);
    const move = (lat, lon, zoom) => {
      if (animate) map.flyTo([lat, lon], zoom, { duration: 0.7 });
      else map.setView([lat, lon], zoom);
    };
    if (city && city.lat != null && city.lon != null) {
      watchPlace(
        { lat: city.lat, lon: city.lon, state: city.s, fips: city.f || "", name: `${city.n}, ${city.s}`, same: "" },
        true
      );
      move(city.lat, city.lon, 9);
      revealLocal();
      return;
    }
    if (county && county.lat != null && county.lon != null) {
      watchPlace(
        { lat: county.lat, lon: county.lon, state: county.s, fips: county.f || "", name: `${county.n} County, ${county.s}`, same: "" },
        true
      );
      move(county.lat, county.lon, 8);
      revealLocal();
      return;
    }
    if (st) {
      watchPlace({ lat: st.lat, lon: st.lon, state: st.abbr, fips: "", name: st.name, same: "" }, true);
      move(st.lat, st.lon, 6);
      revealLocal();
      return;
    }
    state.placePinned = false;
    state.watchKey = "";
    onMapMoved();
  }

  function revealLocal() {
    if (window.innerWidth < 1100) el("sidebar").classList.add("open");
    const panel = el("local-panel");
    if (panel) panel.scrollIntoView({ block: "nearest" });
  }

  function persist() {
    localStorage.setItem(STORE_KEY, JSON.stringify(state.filters));
  }
  function loadFilters() {
    try {
      const raw = JSON.parse(localStorage.getItem(STORE_KEY) || "null");
      if (raw) state.filters = { states: raw.states || [], counties: raw.counties || [], cities: raw.cities || [] };
    } catch (_) {
      /* ignore */
    }
  }

  function connectWs() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => {
      el("ws-dot").className = "dot on";
      el("ws-label").textContent = "live";
    };
    ws.onclose = () => {
      el("ws-dot").className = "dot off";
      el("ws-label").textContent = "reconnecting";
      setTimeout(connectWs, 2500);
    };
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "hello") {
        state.alerts = msg.alerts || [];
        state.knownIds = new Set(state.alerts.map((a) => a.id));
        if (msg.frames) applyFrames(msg.frames);
        if (msg.tracks) applyTracks(msg.tracks);
        if (msg.rotation) applyRotation(msg.rotation);
        renderAlerts();
        el("status-nws").textContent = `NWS ${state.alerts.length} alerts`;
      } else if (msg.type === "alerts") {
        applyAlertDelta(msg);
      } else if (msg.type === "frames") {
        applyFrames(msg.frames);
      } else if (msg.type === "tracks") {
        applyTracks(msg.geojson);
      } else if (msg.type === "rotation") {
        applyRotation(msg);
      } else if (msg.type === "focus" && msg.site) {
        setSite(msg.site);
      }
    };
    state.ws = ws;
  }

  function applyAlertDelta(msg) {
    const byId = new Map(state.alerts.map((a) => [a.id, a]));
    for (const id of msg.expired || []) byId.delete(id);
    for (const a of msg.updated || []) byId.set(a.id, a);
    const fresh = [];
    for (const a of msg.new || []) {
      byId.set(a.id, { ...a, _new: true });
      if (matchesFilters(a) && state.kinds.has(a.kind)) fresh.push(a);
    }
    state.alerts = [...byId.values()];
    renderAlerts();
    el("status-nws").textContent = `NWS ${msg.count ?? state.alerts.length} alerts`;
    if (fresh.length) notifyNew(fresh);
  }

  function matchesFilters(alert) {
    const { states, counties, cities } = state.filters;
    if (!states.length && !counties.length && !cities.length) return true;
    const ugc = new Set(alert.ugc || []);
    if (counties.some((c) => ugc.has(c.u))) return true;
    if (cities.some((c) => ugc.has(c.u))) return true;
    if (cities.some((c) => (alert.areas || "").toLowerCase().includes(c.n.toLowerCase()))) return true;
    if (cities.some((c) => alert.geometry && pointInGeometry(c.lat, c.lon, alert.geometry))) return true;
    if (states.length && !counties.length && !cities.length) {
      return (alert.states || []).some((s) => states.includes(s));
    }
    if (states.length && (alert.states || []).some((s) => states.includes(s)) && !counties.length && !cities.length) {
      return true;
    }
    return false;
  }

  function visibleAlerts() {
    return state.alerts
      .filter((a) => state.kinds.has(a.kind) && matchesFilters(a))
      .sort((a, b) => b.severityRank - a.severityRank);
  }

  function renderAlerts() {
    const list = el("alert-list");
    const items = visibleAlerts();
    el("alert-count").textContent = String(items.length);
    if (!items.length) {
      list.innerHTML = '<div class="empty">No active alerts in the selected area.</div>';
    } else {
      list.innerHTML = items
        .map(
          (a) => `
        <article class="alert-card ${a.kind} ${a._new ? "new" : ""} ${a.id === state.readerId ? "selected" : ""}" data-id="${escapeHtml(a.id)}">
          <div class="ev">${escapeHtml(a.event)}</div>
          <div class="meta">${escapeHtml(shortArea(a.areas))} · ${escapeHtml(a.severity)} · exp ${escapeHtml(fmtTime(a.expires))}</div>
        </article>`
        )
        .join("");
      list.querySelectorAll(".alert-card").forEach((card) => {
        card.addEventListener("click", () => selectAlert(card.dataset.id));
      });
    }
    drawAlertPolygons(items);
    renderTicker();
    if (state.readerOpen && state.readerId) {
      const still = state.alerts.find((a) => a.id === state.readerId);
      if (!still) {
        const body = el("reader-body");
        if (body && !body.querySelector(".expired")) {
          body.insertAdjacentHTML(
            "afterbegin",
            '<div class="expired">This alert has expired. The text stays here until you close it.</div>'
          );
        }
      } else if (alertSig(still) !== state.readerSig) {
        selectAlert(still.id);
      }
    }
  }

  function tickerAlerts() {
    const selected = state.filters.states;
    const us = new Set((state.geo.states || []).map((s) => s.abbr));
    let items = state.alerts.filter((a) => state.kinds.has(a.kind));
    if (selected.length) {
      items = items.filter((a) => (a.states || []).some((s) => selected.includes(s)));
    } else {
      items = items.filter((a) => {
        const land = (a.states || []).some((s) => us.has(s));
        if (!land) return false;
        if (a.kind !== "other") return true;
        return a.severityRank >= 4;
      });
    }
    items.sort((a, b) => b.severityRank - a.severityRank || (a.event || "").localeCompare(b.event || ""));
    return items.slice(0, 80);
  }

  function renderTicker() {
    const items = tickerAlerts();
    const scope = state.filters.states[0] || "US";
    const sig = scope + ":" + items.map((a) => a.id).join("|");
    el("ticker-scope").textContent = scope;
    el("ticker-n").textContent = String(items.length);
    if (sig === state.tickerSig) return;
    if (state.readerOpen) {
      state.tickerHold = sig;
      return;
    }
    state.tickerSig = sig;
    state.tickerHold = "";
    const track = el("ticker-track");
    if (!items.length) {
      track.classList.remove("is-loop");
      track.style.removeProperty("--ticker-dur");
      track.innerHTML = `<span class="ticker-empty">No active alerts${scope === "US" ? " nationwide" : " in " + scope}</span>`;
      return;
    }
    const bits = items
      .map(
        (a) =>
          `<button type="button" class="ticker-item ${a.kind}" data-id="${escapeHtml(a.id)}">` +
          `<span class="t-ev">${escapeHtml(a.event)}</span>` +
          `<span class="t-area">${escapeHtml(shortArea(a.areas))}</span>` +
          `<span class="t-exp">exp ${escapeHtml(fmtTime(a.expires))}</span>` +
          `</button>`
      )
      .join("");
    track.innerHTML = bits + bits;
    track.classList.add("is-loop");
    track.querySelectorAll(".ticker-item").forEach((btn) => {
      let fromPointer = false;
      btn.addEventListener("pointerdown", (e) => {
        if (e.pointerType === "mouse" && e.button !== 0) return;
        e.preventDefault();
        fromPointer = true;
        selectAlert(btn.dataset.id);
      });
      btn.addEventListener("click", () => {
        if (fromPointer) {
          fromPointer = false;
          return;
        }
        selectAlert(btn.dataset.id);
      });
    });
    const dur = Math.max(28, items.length * 4.2);
    track.style.setProperty("--ticker-dur", dur + "s");
  }

  function drawAlertPolygons(items) {
    const next = items.filter((a) => a.geometry);
    const nextIds = new Set(next.map((a) => a.id));
    const have = new Set();
    alertLayer.eachLayer((layer) => {
      const id = layer.feature && layer.feature.properties && layer.feature.properties.id;
      if (!id || !nextIds.has(id)) alertLayer.removeLayer(layer);
      else have.add(id);
    });
    next.forEach((a) => {
      if (have.has(a.id)) return;
      alertLayer.addData({ type: "Feature", geometry: a.geometry, properties: a });
    });
  }

  function alertStyle(feat) {
    const kind = feat.properties.kind || "other";
    const color = feat.properties.color || "#00e5ff";
    return {
      color,
      weight: kind === "tornado" ? 2.4 : 1.6,
      opacity: 0.95,
      fillColor: color,
      fillOpacity: kind === "tornado" ? 0.22 : 0.12,
    };
  }

  function bindAlert(feat, layer) {
    layer.on("click", () => selectAlert(feat.properties.id));
  }

  function alertSig(a) {
    return [a.id, a.expires, a.headline, a.description, a.instruction].join("\n");
  }

  function alertCopy(a) {
    return `
      <span class="pill" style="color:${a.color}">${escapeHtml(a.severity)}</span>
      <span class="pill" style="color:${a.color}">${escapeHtml(a.urgency)}</span>
      <span class="pill" style="color:${a.color}">${escapeHtml(a.certainty || "")}</span>
      <div class="kv">${escapeHtml(a.headline || "")}</div>
      <div class="kv"><b>Areas</b><br>${escapeHtml(a.areas || "—")}</div>
      <div class="kv"><b>Expires</b> ${escapeHtml(fmtFull(a.expires))} · <b>Effective</b> ${escapeHtml(fmtFull(a.effective))}</div>
      <div class="kv"><b>Issued by</b> ${escapeHtml(a.sender || "NWS")}</div>
      ${a.motion ? `<div class="kv"><b>Motion</b> ${escapeHtml(a.motion.dir)} at ${a.motion.mph} mph</div>` : ""}
      <div class="desc">${escapeHtml(a.description || "No description was included with this alert.")}</div>
      ${a.instruction ? `<div class="desc" style="margin-top:10px;color:#ffd60a">${escapeHtml(a.instruction)}</div>` : ""}
    `;
  }

  function selectAlert(id) {
    const a = state.alerts.find((x) => x.id === id);
    if (!a) return;
    const same = state.readerOpen && state.readerId === id;
    const sig = alertSig(a);
    state.readerId = id;
    state.readerOpen = true;
    el("ticker").classList.add("is-held");
    el("reader").hidden = false;
    el("reader-kicker").textContent = `${a.severity} · until ${fmtFull(a.expires)}`;
    el("reader-title").textContent = a.event;
    const body = el("reader-body");
    if (!same || state.readerSig !== sig) {
      const scroll = same ? body.scrollTop : 0;
      const desk = body.querySelector("#reader-desk");
      const keepDesk = same && desk ? desk.innerHTML : "";
      const keepPayload = same && desk ? desk.dataset.payload || "" : "";
      body.innerHTML = `<div class="alert-copy">${alertCopy(a)}</div><div id="reader-desk">${keepDesk}</div>`;
      if (same) body.scrollTop = scroll;
      state.readerSig = sig;
      if (!same) {
        const center = geomCenter(a.geometry);
        loadDesk(el("reader-desk"), {
          lat: center ? center.lat : null,
          lon: center ? center.lon : null,
          state: (a.states && a.states[0]) || "",
          fips: "",
          name: shortArea(a.areas),
          same: (a.same || []).slice(0, 12).join(","),
        });
      } else {
        const next = el("reader-desk");
        if (next) next.dataset.payload = keepPayload;
        wireDesk(next);
      }
    }
    document.querySelectorAll(".alert-card, .ticker-item").forEach((node) => {
      node.classList.toggle("selected", node.dataset.id === id);
    });
    if (!same && a.geometry) {
      try {
        const bounds = L.geoJSON(a.geometry).getBounds();
        const wide = window.innerWidth >= 1100;
        map.fitBounds(bounds.pad(0.12), {
          paddingTopLeft: [wide ? 28 : 16, 72],
          paddingBottomRight: [wide ? 480 : 16, 28],
          maxZoom: 9,
        });
      } catch (_) {
        /* no bounds */
      }
    }
    if (!same) el("reader-close").focus();
  }

  function closeReader() {
    state.readerOpen = false;
    state.readerId = "";
    state.readerSig = "";
    el("reader").hidden = true;
    if (el("shot").hidden) el("ticker").classList.remove("is-held");
    document.querySelectorAll(".alert-card.selected, .ticker-item.selected").forEach((node) => {
      node.classList.remove("selected");
    });
    if (state.tickerHold) {
      state.tickerSig = "";
      renderTicker();
    }
  }

  function closeShot() {
    el("shot").hidden = true;
    el("shot-img").removeAttribute("src");
    if (!state.readerOpen) el("ticker").classList.remove("is-held");
  }

  function openShot(cam) {
    el("shot-title").textContent = cam.direction ? `${cam.name} · ${cam.direction}` : cam.name;
    const img = el("shot-img");
    img.alt = cam.name || "Camera";
    img.src = `${cam.image}${cam.image.includes("?") ? "&" : "?"}t=${Date.now()}`;
    el("shot-meta").innerHTML =
      `${escapeHtml(cam.km != null ? cam.km + " km away" : "")}` +
      `${cam.attribution ? ` · ${escapeHtml(cam.attribution)}` : ""}` +
      (cam.page ? `<br><a href="${escapeHtml(cam.page)}" target="_blank" rel="noopener">Open the official camera page</a>` : "");
    el("shot").hidden = false;
    el("ticker").classList.add("is-held");
    el("shot-close").focus();
  }

  function watchPlace(info, pinned) {
    state.placePinned = pinned;
    const key = [info.name || "", info.state || "", info.fips || "", info.same || "", info.lat != null ? Number(info.lat).toFixed(2) : "", info.lon != null ? Number(info.lon).toFixed(2) : ""].join("|");
    if (key === state.watchKey) return;
    state.watchKey = key;
    const label = info.name || info.state || "map";
    el("local-name").textContent = label.length > 22 ? label.slice(0, 20) + "…" : label;
    loadDesk(el("local-body"), info);
  }

  async function loadDesk(target, info) {
    if (!target) return;
    const seq = String((Number(target.dataset.seq) || 0) + 1);
    target.dataset.seq = seq;
    target.innerHTML = '<p class="empty">Loading weather.gov, NOAA Weather Radio, and cameras…</p>';
    const params = new URLSearchParams();
    if (info.lat != null && info.lon != null && info.lat !== "" && info.lon !== "") {
      params.set("lat", String(info.lat));
      params.set("lon", String(info.lon));
    }
    if (info.state) params.set("state", info.state);
    if (info.fips) params.set("fips", info.fips);
    if (info.name) params.set("name", info.name);
    if (info.same) params.set("same", info.same);
    try {
      const res = await fetch(`/api/place?${params.toString()}`);
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      if (target.dataset.seq !== seq) return;
      target.innerHTML = deskHtml(data, [info.fips, info.same].filter(Boolean).join(","));
      target.dataset.payload = JSON.stringify({
        stations: (data.radio && data.radio.stations) || [],
        shots: (data.cameras && data.cameras.shots) || [],
      });
      wireDesk(target);
      if (target.id === "local-body") {
        drawRadioMarkers((data.radio && data.radio.stations) || []);
        const resolved = data.weather && data.weather.place;
        if (resolved && (!info.name || info.name === "Map center")) el("local-name").textContent = resolved;
      }
    } catch (_) {
      if (target.dataset.seq !== seq) return;
      target.innerHTML = '<p class="empty">Links for this area did not load. Search again in a moment.</p>';
    }
  }

  function preferredSame(station, hint) {
    const codes = station.same || [];
    const wanted = new Set();
    String(hint || "")
      .split(/[^0-9]+/)
      .forEach((raw) => {
        if (raw.length >= 5) wanted.add(raw.slice(-5));
      });
    if (wanted.size) {
      const hit = codes.find((code) => wanted.has(String(code).replace(/\D/g, "").slice(-5)));
      if (hit) return hit;
    }
    return codes[0] || "";
  }

  function deskHtml(data, sameHint) {
    const weather = data.weather || {};
    const radio = data.radio || {};
    const cameras = data.cameras || {};
    const links = (weather.links || [])
      .map(
        (link) =>
          `<a class="link-btn" href="${escapeHtml(link.url)}" target="_blank" rel="noopener">${escapeHtml(link.label)}</a>`
      )
      .join("");
    const stations = radio.stations || [];
    const radios = stations.length
      ? stations
          .map((s, i) => {
            const status = s.status === "NORMAL" ? "on" : s.status === "DEGRADED" ? "degraded" : "down";
            const label = s.status === "NORMAL" ? "On air" : s.status === "DEGRADED" ? "Degraded" : "Out of service";
            const where = s.match === "county" ? "Covers this area" : s.match === "state" ? "In this state" : "Nearest";
            const dist = s.km != null ? ` · ${s.km} km` : "";
            const same = preferredSame(s, sameHint);
            const listen = s.stream && s.stream.url
              ? `<button class="radio-listen" type="button" data-radio="${i}" data-call="${escapeHtml(s.call)}">Listen</button>`
              : "";
            return `<div class="radio-row">
              <a href="${escapeHtml(s.url)}" target="_blank" rel="noopener"><b>${escapeHtml(s.call)} · ${escapeHtml(s.mhz)} MHz</b></a>
              <div class="radio-actions">${listen}<button class="radio-map" type="button" data-radio="${i}">Map</button></div>
              <span class="radio-status ${status}">${label}</span>
              <small>${escapeHtml(s.name || "")}${s.location ? " · " + escapeHtml(s.location) : ""}${dist} · ${where}${s.watts ? ` · ${escapeHtml(String(s.watts))} W` : ""}${same ? ` · SAME ${escapeHtml(same)}` : ""}</small>
            </div>`;
          })
          .join("")
      : '<p class="empty">No NOAA Weather Radio transmitter is listed for this area.</p>';
    const shots = cameras.shots || [];
    const cams = shots.length
      ? `<div class="cam-row">${shots
          .map(
            (cam, i) => `<button class="cam-card" type="button" data-cam="${i}">
              <img src="${escapeHtml(cam.image)}" alt="${escapeHtml(cam.name)}" loading="lazy" />
              <span>${escapeHtml(cam.name)}${cam.direction ? " · " + escapeHtml(cam.direction) : ""}<br><small>${cam.km != null ? escapeHtml(String(cam.km)) + " km · " : ""}${escapeHtml(cam.attribution || "")}</small></span>
            </button>`
          )
          .join("")}</div>`
      : '<p class="empty">No FAA weather camera within 200 km. Road cameras and webcams for this spot are linked below.</p>';
    const portals = (cameras.portals || [])
      .map(
        (link) =>
          `<a class="link-btn" href="${escapeHtml(link.url)}" target="_blank" rel="noopener">${escapeHtml(link.label)}</a>`
      )
      .join("");
    const listing = radio.listing
      ? `<a class="link-btn" href="${escapeHtml(radio.listing)}" target="_blank" rel="noopener">NOAA radio coverage</a>`
      : "";
    return `
      ${weather.summary ? `<p class="local-summary">${escapeHtml(weather.summary)}</p>` : ""}
      <p class="section-label">Weather.gov</p>
      <div class="link-row">${links || '<span class="empty">No forecast point for this spot.</span>'}</div>
      <p class="section-label">NOAA Weather Radio</p>
      <div class="radio-list">${radios}</div>
      <p class="nwr-hint">Listen plays a volunteer stream of that transmitter when one is online. The rest stay as coverage links.</p>
      <div class="link-row">${listing}<a class="link-btn" href="https://www.weather.gov/nwr" target="_blank" rel="noopener">NWR program</a></div>
      <p class="section-label">Cameras</p>
      ${cams}
      <div class="link-row">${portals}</div>
    `;
  }

  function wireDesk(target) {
    if (!target) return;
    let payload = { stations: [], shots: [] };
    try {
      payload = JSON.parse(target.dataset.payload || "{}");
    } catch (_) {
      payload = { stations: [], shots: [] };
    }
    target.querySelectorAll(".radio-listen").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const station = (payload.stations || [])[Number(btn.dataset.radio)];
        if (station) playNwr(station);
      });
    });
    const playing = el("nwr-audio").dataset.src || "";
    if (playing) {
      target.querySelectorAll(".radio-listen").forEach((btn) => {
        const station = (payload.stations || [])[Number(btn.dataset.radio)];
        const stream = station && station.stream;
        if (stream && (stream.url === playing || stream.fallback === playing)) btn.classList.add("is-playing");
      });
    }
    target.querySelectorAll(".radio-map").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const station = (payload.stations || [])[Number(btn.dataset.radio)];
        if (!station || station.lat == null) return;
        map.flyTo([station.lat, station.lon], 9, { duration: 0.6 });
        radioLayer.eachLayer((layer) => {
          const ll = layer.getLatLng && layer.getLatLng();
          if (ll && Math.abs(ll.lat - station.lat) < 0.01 && Math.abs(ll.lng - station.lon) < 0.01) layer.openPopup();
        });
      });
    });
    target.querySelectorAll(".cam-card").forEach((btn) => {
      btn.addEventListener("click", () => {
        const cam = (payload.shots || [])[Number(btn.dataset.cam)];
        if (cam) openShot(cam);
      });
    });
    target.querySelectorAll(".cam-card img").forEach((img) => {
      img.addEventListener("error", () => {
        img.replaceWith(Object.assign(document.createElement("span"), { textContent: "Camera offline" }));
      });
    });
  }

  function drawRadioMarkers(stations) {
    radioLayer.clearLayers();
    (stations || []).forEach((s) => {
      if (s.lat == null || s.lon == null) return;
      const up = s.status === "NORMAL";
      const listen = s.stream && nwrUrlOk(s.stream.url) ? `<br><button type="button" class="nwr-pop">Listen</button>` : "";
      const marker = L.circleMarker([s.lat, s.lon], {
        radius: 7,
        color: up ? "#00e676" : "#ffd60a",
        weight: 2,
        fillColor: up ? "#00e676" : "#ffd60a",
        fillOpacity: 0.25,
      });
      marker.bindPopup(
        `<b>${escapeHtml(s.call)}</b> ${escapeHtml(s.mhz)} MHz<br>${escapeHtml(s.name || "")}<br>${escapeHtml(s.status || "")}<br><a href="${escapeHtml(s.url)}" target="_blank" rel="noopener">NOAA coverage list</a>${listen}`
      );
      if (listen) {
        marker.on("popupopen", () => {
          const btn = marker.getPopup().getElement()?.querySelector(".nwr-pop");
          if (!btn || btn.dataset.bound) return;
          btn.dataset.bound = "1";
          btn.addEventListener("click", (ev) => {
            ev.preventDefault();
            playNwr(s);
          });
        });
      }
      marker.addTo(radioLayer);
    });
  }

  const NWR_HOSTS = new Set([
    "wxradio.org",
    "www.urberg.net",
    "stream.mikev.com",
    "noaaradio.herseyweather.com",
    "broadcast.bismarckweather.net",
    "noaa-manassas-radio.from-va.com",
  ]);

  function nwrUrlOk(url) {
    try {
      const parsed = new URL(url);
      return parsed.protocol === "https:" && NWR_HOSTS.has(parsed.hostname);
    } catch (_) {
      return false;
    }
  }

  function fitPlayer() {
    const box = el("nwr-player");
    document.documentElement.style.setProperty("--player-h", box && !box.hidden ? `${box.offsetHeight}px` : "0px");
  }

  function playNwr(station) {
    const stream = station && station.stream;
    if (!stream || !nwrUrlOk(stream.url)) return;
    const audio = el("nwr-audio");
    const fallback = nwrUrlOk(stream.fallback) ? stream.fallback : "";
    audio.dataset.fallback = fallback;
    audio.dataset.tried = "";
    el("nwr-title").textContent = `${station.call} · ${station.mhz} MHz`;
    el("nwr-meta").textContent = [station.name, station.location].filter(Boolean).join(" · ");
    el("nwr-page").href = typeof stream.page === "string" && stream.page.startsWith("https://")
      ? stream.page
      : "https://noaaweatherradio.org/";
    el("nwr-status").textContent = "Connecting to the volunteer stream…";
    el("nwr-player").hidden = false;
    fitPlayer();
    document.querySelectorAll(".radio-listen.is-playing").forEach((node) => node.classList.remove("is-playing"));
    document.querySelectorAll(".radio-listen").forEach((node) => {
      if (node.dataset.call === station.call) node.classList.add("is-playing");
    });
    if (audio.dataset.src !== stream.url) {
      audio.dataset.src = stream.url;
      audio.src = stream.url;
    }
    const started = audio.play();
    if (started && started.catch) {
      started.catch(() => {
        el("nwr-status").textContent = "Press play on the player to start audio.";
      });
    }
  }

  function onNwrError() {
    const audio = el("nwr-audio");
    const fallback = audio.dataset.fallback || "";
    if (fallback && audio.dataset.tried !== fallback && nwrUrlOk(fallback)) {
      audio.dataset.tried = fallback;
      audio.dataset.src = fallback;
      audio.src = fallback;
      const started = audio.play();
      if (started && started.catch) started.catch(() => {});
      return;
    }
    el("nwr-status").textContent = "This stream is offline right now.";
  }

  function stopNwr() {
    const audio = el("nwr-audio");
    audio.pause();
    audio.removeAttribute("src");
    audio.dataset.src = "";
    audio.load();
    el("nwr-player").hidden = true;
    fitPlayer();
    document.querySelectorAll(".radio-listen.is-playing").forEach((node) => node.classList.remove("is-playing"));
  }

  function geomCenter(geom) {
    if (!geom || !geom.coordinates) return null;
    const coords = [];
    const walk = (node) => {
      if (!node) return;
      if (typeof node[0] === "number" && typeof node[1] === "number") coords.push(node);
      else if (Array.isArray(node)) node.forEach(walk);
    };
    walk(geom.coordinates);
    if (!coords.length) return null;
    let lat = 0;
    let lon = 0;
    coords.forEach(([x, y]) => {
      lon += x;
      lat += y;
    });
    return { lat: lat / coords.length, lon: lon / coords.length };
  }

  function applyFrames(payload) {
    const past = (payload && payload.past) || [];
    const next = past.map((f) => {
      const hash = (f.path || "").split("/").pop();
      return { time: f.time, hash, path: f.path };
    });
    const sig = next.map((f) => f.hash).join(",");
    state.frames = next;
    if (sig !== state.frameSig) {
      state.frameSig = sig;
      rebuildRadar();
    }
    el("status-radar").textContent = `Radar ${state.frames.length} frames`;
  }

  function radarShown() {
    return state.layer === "reflectivity" || state.layer === "tracks";
  }

  function radarOpacity() {
    return state.layer === "tracks" ? state.opacity * 0.45 : state.opacity;
  }

  function rebuildRadar() {
    const keep = new Map(radarLayers.map((l) => [l._sdHash, l]));
    const next = [];
    const show = radarShown();
    state.frames.forEach((f) => {
      let layer = keep.get(f.hash);
      if (!layer) {
        layer = L.tileLayer(`/api/radar/reflectivity/${f.hash}/{z}/{x}/{y}.png`, {
          opacity: 0,
          pane: "overlayPane",
          maxNativeZoom: 7,
          maxZoom: 12,
          className: "radar-tiles",
        });
        layer._sdHash = f.hash;
      } else {
        keep.delete(f.hash);
      }
      if (show && !map.hasLayer(layer)) layer.addTo(map);
      if (!show && map.hasLayer(layer)) map.removeLayer(layer);
      next.push(layer);
    });
    keep.forEach((layer) => {
      if (map.hasLayer(layer)) map.removeLayer(layer);
    });
    radarLayers = next;
    const slider = el("loop-slider");
    slider.max = Math.max(0, radarLayers.length - 1);
    if (!radarLayers.length) return;
    if (state.frameIndex >= radarLayers.length) state.frameIndex = radarLayers.length - 1;
    showFrame(state.frameIndex, true);
    applySiteOverlays();
    updateSweep();
  }

  function showFrame(i, instant) {
    if (!radarLayers.length) return;
    const next = Math.max(0, Math.min(i, radarLayers.length - 1));
    const prev = state.frameIndex;
    const op = radarOpacity();
    const show = radarShown();
    if (fadeTimer) {
      clearInterval(fadeTimer);
      fadeTimer = null;
    }
    state.frameIndex = next;
    const f = state.frames[next];
    el("loop-time").textContent = f ? fmtUnix(f.time) : "--:--";
    el("loop-slider").value = String(next);

    if (!show) {
      radarLayers.forEach((layer) => layer.setOpacity(0));
      return;
    }
    radarLayers.forEach((layer) => {
      if (!map.hasLayer(layer)) layer.addTo(map);
    });
    if (instant || prev === next || !radarLayers[prev]) {
      radarLayers.forEach((layer, idx) => layer.setOpacity(idx === next ? op : 0));
      return;
    }
    const from = radarLayers[prev];
    const to = radarLayers[next];
    let step = 0;
    const steps = 10;
    fadeTimer = setInterval(() => {
      step += 1;
      const k = step / steps;
      to.setOpacity(op * k);
      from.setOpacity(op * (1 - k));
      if (step >= steps) {
        clearInterval(fadeTimer);
        fadeTimer = null;
        radarLayers.forEach((layer, idx) => layer.setOpacity(idx === next ? op : 0));
      }
    }, 50);
  }

  function startLoop() {
    if (loopTimer) clearInterval(loopTimer);
    loopTimer = setInterval(() => {
      if (!state.playing || state.layer !== "reflectivity" || radarLayers.length < 2) return;
      const last = radarLayers.length - 1;
      if (state.frameIndex >= last) {
        holdTicks += 1;
        if (holdTicks < 5) return;
        holdTicks = 0;
        showFrame(0);
        return;
      }
      holdTicks = 0;
      showFrame(state.frameIndex + 1);
    }, 850);
  }

  function applyOpacity() {
    if (state.layer === "reflectivity") showFrame(state.frameIndex);
    if (velocityLayer) velocityLayer.setOpacity(state.opacity);
    if (nstLayer) nstLayer.setOpacity(state.opacity);
  }

  function setLayer(name) {
    state.layer = name;
    document.querySelectorAll("[data-layer]").forEach((b) => b.classList.toggle("active", b.dataset.layer === name));
    applySiteOverlays();
    showFrame(state.frameIndex);
  }

  function applySiteOverlays() {
    if (velocityLayer) {
      map.removeLayer(velocityLayer);
      velocityLayer = null;
    }
    if (nstLayer) {
      map.removeLayer(nstLayer);
      nstLayer = null;
    }
    const site = state.site && state.site.id;
    if (!site) return;
    if (state.layer === "velocity") {
      velocityLayer = L.tileLayer(`/api/radar/velocity/${site}/{z}/{x}/{y}.png`, {
        opacity: state.opacity,
        maxZoom: 12,
      }).addTo(map);
    }
  }

  function applyTracks(geojson) {
    state.tracks = geojson || { features: [] };
    const features = state.tracks.features || [];
    const sig = features
      .map((f) => {
        const p = f.properties || {};
        const c = (f.geometry && f.geometry.coordinates) || [0, 0];
        return `${p.stormId}:${p.nexrad}:${Number(c[0]).toFixed(2)}:${Number(c[1]).toFixed(2)}:${p.kind}`;
      })
      .join("|");
    if (sig === state.trackSig) {
      el("status-radar").textContent = `Radar · ${features.length} cells`;
      return;
    }
    state.trackSig = sig;
    trackLayer.clearLayers();
    el("status-radar").textContent = `Radar · ${features.length} cells`;
    features.forEach((feat) => {
      const p = feat.properties || {};
      const [lon, lat] = feat.geometry.coordinates;
      const kind = p.kind || "cell";
      const icon = L.divIcon({ className: "", html: `<div class="track-dot ${kind}"></div>`, iconSize: [10, 10] });
      L.marker([lat, lon], { icon }).bindTooltip(trackLabel(p), { direction: "top" }).addTo(trackLayer);
      const future = (p.track && p.track.future) || [];
      const past = (p.track && p.track.past) || [];
      if (past.length > 1) {
        L.polyline(
          past.map((c) => [c[1], c[0]]),
          { color: "#8b9bb0", weight: 1.5, dashArray: "3 6", opacity: 0.7 }
        ).addTo(trackLayer);
      }
      if (future.length > 1) {
        const line = L.polyline(
          future.map((c) => [c[1], c[0]]),
          { color: kind === "tvs" ? "#ff2d55" : kind === "meso" ? "#ff6b00" : "#ffd60a", weight: 2, className: "storm-path" }
        ).addTo(trackLayer);
        animateLine(line);
      }
    });
    drawAlertMotion();
  }

  function drawAlertMotion() {
    visibleAlerts().forEach((a) => {
      if (!a.motion || !a.geometry) return;
      const b = L.geoJSON(a.geometry).getBounds();
      const c = b.getCenter();
      const km = a.motion.mph * 1.60934;
      const dest = destPoint(c.lat, c.lng, a.motion.deg, km);
      L.polyline(
        [
          [c.lat, c.lng],
          dest,
        ],
        { color: a.color, weight: 2, dashArray: "6 8" }
      ).addTo(trackLayer);
    });
  }

  function animateLine(line) {
    const eline = line.getElement && line.getElement();
    if (!eline) return;
    eline.style.strokeDasharray = "10 14";
    eline.style.animation = "dash 1.2s linear infinite";
  }

  function trackLabel(p) {
    const bits = [p.nexrad, p.stormId, p.kind];
    if (p.sknt) bits.push(`${Math.round(p.sknt)} kt`);
    if (p.maxDbz) bits.push(`${p.maxDbz} dBZ`);
    if (p.tvs && p.tvs !== "N") bits.push("TVS");
    if (p.meso && p.meso !== "N") bits.push(`MESO ${p.meso}`);
    return bits.filter(Boolean).join(" · ");
  }

  function applyRotation(msg) {
    if (msg.site) setSite(msg.site);
    const sigs = msg.signatures || [];
    const sig = sigs.map((s) => `${s.kind}:${s.lat.toFixed(3)}:${s.lon.toFixed(3)}`).join("|");
    if (sig === state.rotSig) {
      el("status-rot").textContent = msg.ok
        ? `${msg.site?.id || "NEXRAD"} · ${sigs.length} rotation`
        : `Rotation ${msg.error ? "offline" : "idle"}`;
      return;
    }
    state.rotSig = sig;
    rotLayer.clearLayers();
    state.rotation = sigs;
    el("status-rot").textContent = msg.ok
      ? `${msg.site?.id || "NEXRAD"} · ${sigs.length} rotation`
      : `Rotation ${msg.error ? "offline" : "idle"}`;
    sigs.forEach((s) => {
      const icon = L.divIcon({
        className: "",
        html: `<div class="rot-icon ${s.kind}"></div>`,
        iconSize: [22, 22],
        iconAnchor: [11, 11],
      });
      L.marker([s.lat, s.lon], { icon, zIndexOffset: 400 })
        .bindPopup(
          `<b>${s.label}</b><br>ΔV ${s.deltaMs} m/s · shear ${s.shear} /s<br>${s.rangeKm} km @ ${s.azimuth}°`
        )
        .addTo(rotLayer);
    });
  }

  function setSite(site) {
    const changed = !state.site || state.site.id !== site.id;
    state.site = site;
    el("site-chip").textContent = `NEXRAD ${site.id} · ${site.name}`;
    if (changed) {
      applySiteOverlays();
      rotLayer.clearLayers();
      state.rotation = [];
      state.rotSig = "";
      el("status-rot").textContent = `Rotation ${site.id} …`;
    }
    updateSweep();
  }

  function ensureSweep() {
    if (sweepEl) return sweepEl;
    sweepEl = L.DomUtil.create("div", "nexrad-sweep", map.getPane("sweepPane"));
    sweepEl.innerHTML = '<div class="nexrad-sweep-beam"></div><div class="nexrad-sweep-rings"></div>';
    return sweepEl;
  }

  function updateSweep() {
    const node = ensureSweep();
    const site = state.site;
    if (!site) {
      node.style.display = "none";
      return;
    }
    node.style.display = "block";
    const origin = map.latLngToLayerPoint([site.lat, site.lon]);
    const east = destPoint(site.lat, site.lon, 90, 230);
    const edge = map.latLngToLayerPoint(east);
    const r = Math.max(48, Math.hypot(edge.x - origin.x, edge.y - origin.y));
    node.style.width = `${r * 2}px`;
    node.style.height = `${r * 2}px`;
    L.DomUtil.setPosition(node, L.point(origin.x - r, origin.y - r));
  }

  async function onMapMoved() {
    const c = map.getCenter();
    try {
      const site = await (await fetch(`/api/nexrad/nearest?lat=${c.lat}&lon=${c.lng}`)).json();
      setSite(site);
      if (!state.placePinned) {
        watchPlace(
          {
            lat: Math.round(c.lat * 10) / 10,
            lon: Math.round(c.lng * 10) / 10,
            state: "",
            fips: "",
            name: "Map center",
            same: "",
          },
          false
        );
      }
      if (state.ws && state.ws.readyState === 1) {
        state.ws.send(JSON.stringify({ type: "focus", lat: c.lat, lon: c.lng }));
      }
    } catch (_) {
      /* ignore */
    }
  }

  async function refreshRest() {
    try {
      const [alerts, frames, tracks] = await Promise.all([
        fetch("/api/alerts").then((r) => r.json()),
        fetch("/api/radar/frames").then((r) => r.json()),
        fetch("/api/radar/tracks").then((r) => r.json()),
      ]);
      if (alerts.alerts) {
        state.alerts = alerts.alerts;
        renderAlerts();
      }
      if (frames && frames.past) applyFrames(frames);
      if (tracks) applyTracks(tracks);
    } catch (_) {
      /* websocket is primary */
    }
  }

  function useLocation() {
    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition((pos) => {
      const lat = pos.coords.latitude;
      const lon = pos.coords.longitude;
      map.flyTo([lat, lon], 8);
      watchPlace({ lat, lon, state: "", fips: "", name: "My location", same: "" }, true);
    });
  }

  async function enableNotify() {
    if (!("Notification" in window)) return;
    const perm = await Notification.requestPermission();
    state.notify = perm === "granted";
    el("notify-btn").textContent = state.notify ? "Alerts on" : "Enable alerts";
    ping();
  }

  function notifyNew(fresh) {
    ping(fresh.some((a) => a.kind === "tornado") ? "tornado" : "warn");
    if (!state.notify) return;
    const top = fresh.sort((a, b) => b.severityRank - a.severityRank)[0];
    try {
      new Notification(`${top.event}`, { body: top.headline || top.areas, silent: true });
    } catch (_) {
      /* ignored */
    }
  }

  function ping(kind) {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const beep = (freq, t0, dur) => {
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.type = "sine";
      o.frequency.setValueAtTime(freq, t0);
      g.gain.setValueAtTime(0.0001, t0);
      g.gain.exponentialRampToValueAtTime(0.18, t0 + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
      o.connect(g);
      g.connect(ctx.destination);
      o.start(t0);
      o.stop(t0 + dur + 0.02);
    };
    const now = ctx.currentTime;
    if (kind === "tornado") {
      beep(880, now, 0.18);
      beep(660, now + 0.2, 0.18);
      beep(990, now + 0.4, 0.28);
    } else {
      beep(740, now, 0.16);
      beep(520, now + 0.18, 0.22);
    }
  }

  function pointInGeometry(lat, lon, geom) {
    if (!geom) return false;
    const type = geom.type;
    const ringHit = (rings) => rings.some((poly) => pointInRing(lat, lon, poly[0] || poly));
    if (type === "Polygon") return pointInRing(lat, lon, geom.coordinates[0]);
    if (type === "MultiPolygon") return geom.coordinates.some((poly) => pointInRing(lat, lon, poly[0]));
    return ringHit(geom.coordinates || []);
  }

  function pointInRing(lat, lon, ring) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const xi = ring[i][0],
        yi = ring[i][1];
      const xj = ring[j][0],
        yj = ring[j][1];
      const intersect = yi > lat !== yj > lat && lon < ((xj - xi) * (lat - yi)) / (yj - yi + 1e-12) + xi;
      if (intersect) inside = !inside;
    }
    return inside;
  }

  function destPoint(lat, lon, deg, km) {
    const r = 6371;
    const brng = (deg * Math.PI) / 180;
    const φ1 = (lat * Math.PI) / 180;
    const λ1 = (lon * Math.PI) / 180;
    const φ2 = Math.asin(Math.sin(φ1) * Math.cos(km / r) + Math.cos(φ1) * Math.sin(km / r) * Math.cos(brng));
    const λ2 =
      λ1 +
      Math.atan2(Math.sin(brng) * Math.sin(km / r) * Math.cos(φ1), Math.cos(km / r) - Math.sin(φ1) * Math.sin(φ2));
    return [(φ2 * 180) / Math.PI, (λ2 * 180) / Math.PI];
  }

  function refreshCameraStills() {
    const stamp = Math.floor(Date.now() / 60000);
    document.querySelectorAll(".cam-card img").forEach((img) => {
      const base = (img.getAttribute("src") || "").split("?")[0];
      if (base) img.src = `${base}?t=${stamp}`;
    });
    const shot = el("shot-img");
    if (shot && !el("shot").hidden && shot.src) {
      const base = shot.src.split("?")[0];
      shot.src = `${base}?t=${stamp}`;
    }
  }

  function fmtTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  function fmtFull(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleString();
  }
  function fmtUnix(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  function shortArea(s) {
    if (!s) return "—";
    return s.length > 64 ? s.slice(0, 61) + "…" : s;
  }
  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function debounce(fn, ms) {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), ms);
    };
  }

  const style = document.createElement("style");
  style.textContent = `@keyframes dash { to { stroke-dashoffset: -24; } }`;
  document.head.appendChild(style);
})();
