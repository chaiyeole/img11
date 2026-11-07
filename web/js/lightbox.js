const Lightbox = (() => {
  let overlayEl, imgEl, captionEl, prevBtn, nextBtn, closeBtn;
  let photos = [];
  let index = 0;
  let albumName = "";
  let touchStartX = 0;
  let touchStartY = 0;

  function init() {
    overlayEl = document.getElementById("lightbox");
    if (!overlayEl) return;
    imgEl = document.getElementById("lightbox-img");
    captionEl = document.getElementById("lightbox-caption");
    prevBtn = overlayEl.querySelector(".lightbox-nav.prev");
    nextBtn = overlayEl.querySelector(".lightbox-nav.next");
    closeBtn = overlayEl.querySelector(".lightbox-close");

    prevBtn.addEventListener("click", prev);
    nextBtn.addEventListener("click", next);
    closeBtn.addEventListener("click", close);
    overlayEl.addEventListener("click", (e) => {
      if (e.target === overlayEl) close();
    });
    document.addEventListener("keydown", (e) => {
      if (!overlayEl.classList.contains("open")) return;
      if (e.key === "Escape") close();
      else if (e.key === "ArrowLeft") prev();
      else if (e.key === "ArrowRight") next();
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
    document.addEventListener("fullscreenchange", () => {
      if (!document.fullscreenElement && overlayEl.classList.contains("open")) {
        close();
      }
    });
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
    imgEl.src = photo.full;
    imgEl.alt = photo.slug;
    if (captionEl) captionEl.textContent = `${albumName} — ${index + 1} / ${photos.length}`;
  }

  function open(photoList, startIndex, name) {
    photos = photoList;
    index = startIndex;
    albumName = name || "";
    overlayEl.classList.add("open");
    show();
    enterFullscreen();
  }

  function close() {
    overlayEl.classList.remove("open");
    imgEl.src = "";
    exitFullscreen();
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
