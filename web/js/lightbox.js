const Lightbox = (() => {
  const INFO_KEY = "img11.showInfo";
  const HIST_BINS = 64;
  // Above 2x, extra pixel density adds no visible detail at viewing distance.
  const MAX_DPR = 2;
  // Slideshow: how long each photo stays up, counted from when it has loaded.
  const SLIDE_MS = 5000;
  const SIZE_NAMES = [
    [1600, "phone version"],
    [2200, "1080p version"],
    [Infinity, "2K / 4K version"],
  ];

  // Rows of the info panel, top to bottom. Each shows only when the photo has
  // that value. Hints are written for people new to photography.
  const FIELDS = [
    { key: "camera", label: "Camera" },
    { key: "film", label: "Film simulation", hint: "Fujifilm's in-camera colour recipe", sub: "recipe" },
    { key: "focal", label: "Focal length", hint: "How zoomed in — higher looks closer and flatter" },
    { key: "aperture", label: "Aperture", hint: "Lens opening — smaller number, blurrier background" },
    { key: "shutter", label: "Shutter speed", hint: "How long light came in — faster freezes motion" },
    { key: "iso", label: "ISO", hint: "Sensor sensitivity — higher is brighter but grainier" },
    { key: "ev", label: "Exposure compensation", hint: "Made brighter (+) or darker (−) than the camera's guess" },
    { key: "taken", label: "Taken", format: formatTaken },
  ];

  let overlayEl, imgEl, titleEl, counterEl, infoEl, infoBtn, prevBtn, nextBtn, closeBtn, playBtn, progressEl;
  let slideshow = false; // opened as a slideshow (play controls shown)
  let playing = false;
  let slideTimer = null;
  let slideStartedAt = 0; // when the running countdown (re)started
  let slideLeft = SLIDE_MS; // ms the running countdown had when it (re)started
  let pausedLeft = null; // ms left on this photo when paused mid-countdown
  let flashEl;
  let showInfo = false;
  let photos = [];
  let index = 0;
  let albumName = "";
  let touchStartX = 0;
  let touchStartY = 0;
  let prefetched = [];
  let historyEntry = false; // true while our pushed history entry is current
  let shown = null; // { url, edge } of the size currently displayed
  const histogramCache = new Map();

  function init() {
    overlayEl = document.getElementById("lightbox");
    if (!overlayEl) return;
    imgEl = document.getElementById("lightbox-img");
    // Photos come from another site (the image host); "anonymous" lets the
    // histogram read their pixels. The host must send Access-Control-Allow-Origin.
    imgEl.crossOrigin = "anonymous";
    titleEl = document.getElementById("lightbox-caption");
    counterEl = document.getElementById("lightbox-counter");
    infoEl = document.getElementById("lightbox-info");
    infoBtn = overlayEl.querySelector(".lightbox-info-toggle");
    prevBtn = overlayEl.querySelector(".lightbox-nav.prev");
    nextBtn = overlayEl.querySelector(".lightbox-nav.next");
    closeBtn = overlayEl.querySelector(".lightbox-close");
    playBtn = overlayEl.querySelector(".lightbox-play");
    progressEl = overlayEl.querySelector(".lightbox-progress");
    flashEl = overlayEl.querySelector(".lightbox-flash");
    if (playBtn) playBtn.addEventListener("click", togglePlay);
    // In a slideshow, clicking / tapping the photo pauses or resumes it.
    imgEl.addEventListener("click", () => {
      if (slideshow) togglePlay();
    });

    prevBtn.addEventListener("click", prev);
    nextBtn.addEventListener("click", next);
    closeBtn.addEventListener("click", close);
    if (infoBtn) infoBtn.addEventListener("click", toggleInfo);
    if (infoEl) {
      // Small screens hide the hints; tapping a row reveals its hint.
      infoEl.addEventListener("click", (e) => {
        const row = e.target.closest(".info-row");
        if (row) row.classList.toggle("show-hint");
      });
    }
    imgEl.addEventListener("load", () => {
      if (showInfo) renderHistogram(photos[index]);
      imgEl.classList.remove("fading");
      scheduleNext();
    });
    // A photo that fails to load shouldn't stall the slideshow.
    imgEl.addEventListener("error", () => {
      imgEl.classList.remove("fading");
      scheduleNext();
    });
    // Rotating a phone or moving the window to a bigger monitor: upgrade to a
    // larger file if the new screen needs one (never downgrade mid-view).
    let resizeTimer;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (!overlayEl.classList.contains("open") || !shown) return;
        const pick = chooseSize(photos[index]);
        if (pick.edge > shown.edge) {
          shown = pick;
          imgEl.src = pick.url;
          renderInfo(photos[index]);
        }
      }, 250);
    });
    showInfo = readInfoPref();

    overlayEl.addEventListener("click", (e) => {
      if (e.target === overlayEl) close();
    });
    document.addEventListener("keydown", (e) => {
      if (!overlayEl.classList.contains("open")) return;
      if (e.key === "Escape") close();
      else if (e.key === "ArrowLeft") prev();
      else if (e.key === "ArrowRight") next();
      else if (e.key === "i" || e.key === "I") toggleInfo();
      else if (slideshow && (e.key === " " || e.key === "k" || e.key === "K")) {
        e.preventDefault();
        togglePlay();
      }
    });
    overlayEl.addEventListener("touchstart", (e) => {
      touchStartX = e.changedTouches[0].clientX;
      touchStartY = e.changedTouches[0].clientY;
    });
    overlayEl.addEventListener("touchend", (e) => {
      const dx = e.changedTouches[0].clientX - touchStartX;
      const dy = e.changedTouches[0].clientY - touchStartY;
      if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy)) {
        if (dx < 0) next();
        else prev();
      }
    });
    // Browser back (button, touchpad swipe, mobile gesture) closes the photo
    // and stays on the album / tag grid instead of leaving the page.
    window.addEventListener("popstate", () => {
      if (historyEntry) {
        historyEntry = false;
        close();
      }
    });
    document.addEventListener("fullscreenchange", () => {
      if (!document.fullscreenElement && overlayEl.classList.contains("open")) {
        close();
      }
    });
  }

  // Off by default; remembered per visitor. Storage can be unavailable
  // (private mode, blocked site data), in which case it just isn't remembered.
  function readInfoPref() {
    try {
      return localStorage.getItem(INFO_KEY) === "1";
    } catch {
      return false;
    }
  }

  function toggleInfo() {
    showInfo = !showInfo;
    try {
      localStorage.setItem(INFO_KEY, showInfo ? "1" : "0");
    } catch {
      /* not remembered, still toggles for this visit */
    }
    if (overlayEl.classList.contains("open")) renderInfo(photos[index]);
  }

  // One file per kind of screen, as decided for the site:
  //   phone (narrow screen)                -> 1600px
  //   up to ~1080p (<= 2200 real pixels)   -> 2200px
  //   2K / 1440p, Retina laptops, 4K       -> 3840px, the best quality
  // "Real pixels" is the screen's long side times its pixel density (capped
  // at 2x), so a 1080p laptop set to 125% scaling still counts as 1080p.
  const PHONE_MAX_SHORT_SIDE = 600; // CSS px; tablets and up are wider than this

  function chooseSize(photo) {
    const sizes = photo.sizes && photo.sizes.length ? photo.sizes : null;
    if (!sizes) return { url: photo.full, edge: 0 };
    const sw = Math.max(screen.width, window.innerWidth);
    const sh = Math.max(screen.height, window.innerHeight);
    const dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR);
    let target;
    if (Math.min(sw, sh) < PHONE_MAX_SHORT_SIDE) target = 1600;
    else if (Math.max(sw, sh) * dpr <= 2200) target = 2200;
    else target = Infinity;
    // The biggest file up to the target (a photo whose original is small
    // may not have the larger sizes).
    const fitting = sizes.filter((s) => s.edge <= target);
    return fitting.length ? fitting[fitting.length - 1] : sizes[0];
  }

  function sizeLabel(edge, aspect) {
    if (!edge) return null;
    const a = aspect || 1.5;
    const w = Math.round(a >= 1 ? edge : edge * a);
    const h = Math.round(a >= 1 ? edge / a : edge);
    const name = SIZE_NAMES.find(([max]) => edge <= max)[1];
    return `${w} × ${h} · ${name}`;
  }

  // "2026-11-04T07:20" -> "4 Nov 2026, 7:20 am"; date-only values -> "4 Nov 2026".
  function formatTaken(value) {
    const hasTime = value.includes("T");
    const d = new Date(hasTime ? value : `${value}T00:00`);
    const date = d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
    if (!hasTime) return date;
    const time = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }).toLowerCase();
    return `${date}, ${time}`;
  }

  function row(label, value, hint, extraClass) {
    const div = document.createElement("div");
    div.className = `info-row${extraClass ? ` ${extraClass}` : ""}`;
    const dt = document.createElement("dt");
    dt.className = "label";
    dt.textContent = label;
    div.appendChild(dt);
    const dd = document.createElement("dd");
    if (value instanceof Node) dd.appendChild(value);
    else dd.textContent = value;
    div.appendChild(dd);
    if (hint) {
      const h = document.createElement("dd");
      h.className = "info-hint";
      h.textContent = hint;
      div.appendChild(h);
    }
    return div;
  }

  function renderInfo(photo) {
    overlayEl.classList.toggle("info-on", showInfo);
    if (infoBtn) infoBtn.setAttribute("aria-pressed", String(showInfo));
    if (!infoEl) return;
    infoEl.hidden = !showInfo;
    if (!showInfo) return;

    const m = (photo && photo.meta) || {};
    infoEl.innerHTML = "";

    const hist = document.createElement("div");
    hist.className = "histogram";
    infoEl.appendChild(row("Histogram", hist, "Brightness spread — left: shadows, right: highlights", "info-histogram"));

    for (const field of FIELDS) {
      if (!m[field.key]) continue;
      const value = field.format ? field.format(m[field.key]) : m[field.key];
      const r = row(field.label, value, field.hint);
      if (field.sub && m[field.sub]) {
        const sub = document.createElement("dd");
        sub.className = "info-sub";
        sub.textContent = m[field.sub];
        r.insertBefore(sub, r.querySelector(".info-hint"));
      }
      infoEl.appendChild(r);
    }
    if (!FIELDS.some((f) => m[f.key])) {
      infoEl.appendChild(row("Camera", "No camera data for this photo"));
    }
    const resolution = shown && sizeLabel(shown.edge, photo && photo.aspect);
    if (resolution) {
      infoEl.appendChild(row("Shown at", resolution, "Picked for this screen — bigger screens get a sharper file"));
    }

    if (imgEl.complete && imgEl.naturalWidth) renderHistogram(photo);
  }

  // Luminance histogram of the displayed photo, drawn as a small area chart.
  // Computed in the browser from the image that's already loaded, so it costs
  // no extra download and nothing in the manifest.
  function renderHistogram(photo) {
    const target = infoEl && infoEl.querySelector(".histogram");
    if (!target || !photo) return;
    const key = shown ? shown.url : photo.full;
    let bins = histogramCache.get(key);
    if (!bins) {
      try {
        const size = 96;
        const canvas = document.createElement("canvas");
        canvas.width = size;
        canvas.height = size;
        const ctx = canvas.getContext("2d", { willReadFrequently: true });
        ctx.drawImage(imgEl, 0, 0, size, size);
        const data = ctx.getImageData(0, 0, size, size).data;
        bins = new Array(HIST_BINS).fill(0);
        for (let i = 0; i < data.length; i += 4) {
          const lum = 0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2];
          bins[Math.min(HIST_BINS - 1, Math.floor((lum / 256) * HIST_BINS))]++;
        }
        histogramCache.set(key, bins);
      } catch {
        target.closest(".info-row").hidden = true;
        return;
      }
    }
    // sqrt scale so one huge spike (e.g. a black sky) doesn't flatten the rest.
    const scaled = bins.map(Math.sqrt);
    const peak = Math.max(...scaled) || 1;
    const w = 160;
    const h = 36;
    const step = w / (HIST_BINS - 1);
    let d = `M0 ${h}`;
    scaled.forEach((v, i) => {
      d += ` L${(i * step).toFixed(1)} ${(h - (v / peak) * h).toFixed(1)}`;
    });
    d += ` L${w} ${h} Z`;
    target.innerHTML =
      `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" preserveAspectRatio="none" aria-hidden="true">` +
      `<path d="${d}"></path></svg>`;
  }

  // --- Slideshow -----------------------------------------------------------
  // The timer starts only once the photo is on screen, so a slow download
  // never cuts a photo short. Manual prev/next restarts the countdown; pause
  // freezes it (and the ring) and play carries on from where it stopped.
  function scheduleNext() {
    pausedLeft = null;
    startCountdown(SLIDE_MS, true);
  }

  function startCountdown(ms, restartRing) {
    clearTimeout(slideTimer);
    slideTimer = null;
    if (!slideshow || !playing || !overlayEl.classList.contains("open")) return;
    if (restartRing) restartProgress();
    if (progressEl) progressEl.style.animationPlayState = "running";
    slideStartedAt = performance.now();
    slideLeft = ms;
    slideTimer = setTimeout(() => {
      if (photos.length > 1) next();
    }, ms);
  }

  function stopCountdown() {
    clearTimeout(slideTimer);
    slideTimer = null;
    pausedLeft = null;
    if (progressEl) progressEl.classList.remove("run");
  }

  function restartProgress() {
    if (!progressEl) return;
    progressEl.classList.remove("run");
    progressEl.getBoundingClientRect(); // reflow so the animation starts over
    progressEl.style.animationDuration = `${SLIDE_MS}ms`;
    progressEl.classList.add("run");
  }

  function setPlaying(on) {
    playing = on;
    overlayEl.classList.toggle("paused", !on);
    if (playBtn) {
      playBtn.setAttribute("aria-pressed", String(on));
      playBtn.setAttribute("aria-label", on ? "Pause slideshow (space)" : "Play slideshow (space)");
    }
    if (!on) {
      // Freeze mid-countdown: remember the time left and hold the ring.
      if (slideTimer) {
        pausedLeft = Math.max(0, slideLeft - (performance.now() - slideStartedAt));
        clearTimeout(slideTimer);
        slideTimer = null;
        if (progressEl) progressEl.style.animationPlayState = "paused";
      }
    } else if (pausedLeft !== null) {
      const left = pausedLeft;
      pausedLeft = null;
      startCountdown(left, false);
    } else if (imgEl.complete && imgEl.naturalWidth) {
      scheduleNext();
    }
  }

  function togglePlay() {
    if (!slideshow) return;
    setPlaying(!playing);
    flash(playing);
  }

  // Centre sign: a brief pause or play sign that fades away.
  function flash(nowPlaying) {
    if (!flashEl) return;
    flashEl.classList.remove("show-pause", "show-play");
    flashEl.getBoundingClientRect(); // restart the fade if clicked quickly
    flashEl.classList.add(nowPlaying ? "show-play" : "show-pause");
  }

  function enterFullscreen() {
    const request = overlayEl.requestFullscreen || overlayEl.webkitRequestFullscreen;
    if (request) request.call(overlayEl).catch(() => {});
  }

  function exitFullscreen() {
    const exit = document.exitFullscreen || document.webkitExitFullscreen;
    if (document.fullscreenElement && exit) exit.call(document).catch(() => {});
  }

  function show() {
    const photo = photos[index];
    shown = chooseSize(photo);
    stopCountdown();
    // In a slideshow, fade from black into each photo.
    if (slideshow && imgEl.src !== shown.url) imgEl.classList.add("fading");
    imgEl.src = shown.url;
    imgEl.alt = photo.slug;
    if (titleEl) titleEl.textContent = albumName;
    if (counterEl) {
      // In a tag view the title is the tag, so also say which folder this photo lives in.
      const source = photo.albumName && photo.albumName !== albumName ? `  ·  ${photo.albumName}` : "";
      counterEl.textContent = `${index + 1} / ${photos.length}${source}`;
    }
    renderInfo(photo);
    prefetchNeighbors();
    trackImageView(photo);
  }

  function trackImageView(photo) {
    (window.dataLayer = window.dataLayer || []).push({
      event: "image_view",
      album_name: photo.albumName || albumName,
      photo_slug: photo.slug,
    });
  }

  function prefetchNeighbors() {
    if (photos.length < 2) return;
    const nextIdx = (index + 1) % photos.length;
    const prevIdx = (index - 1 + photos.length) % photos.length;
    prefetched = [nextIdx, prevIdx].map((i) => {
      const img = new Image();
      img.crossOrigin = "anonymous"; // same request mode as the viewer, so the cached copy is reused
      img.src = chooseSize(photos[i]).url;
      return img;
    });
  }

  function open(photoList, startIndex, name, options = {}) {
    photos = photoList;
    index = startIndex;
    albumName = name || "";
    slideshow = Boolean(options.slideshow);
    overlayEl.classList.toggle("slideshow", slideshow);
    overlayEl.classList.add("open");
    setPlaying(slideshow);
    if (!historyEntry) {
      history.pushState({ lightbox: true }, "");
      historyEntry = true;
    }
    show();
    enterFullscreen();
  }

  function close() {
    if (!overlayEl.classList.contains("open")) return;
    overlayEl.classList.remove("open", "slideshow", "paused");
    if (flashEl) flashEl.classList.remove("show-pause", "show-play");
    slideshow = false;
    playing = false;
    stopCountdown();
    imgEl.classList.remove("fading");
    imgEl.src = "";
    shown = null;
    exitFullscreen();
    // Closed via ×, Esc, background tap or leaving fullscreen: drop the
    // history entry we added so a later back doesn't just reopen nothing.
    if (historyEntry) {
      historyEntry = false;
      history.back();
    }
  }

  function prev() {
    index = (index - 1 + photos.length) % photos.length;
    show();
  }

  function next() {
    index = (index + 1) % photos.length;
    show();
  }

  document.addEventListener("DOMContentLoaded", init);

  return { open, close, prev, next };
})();
