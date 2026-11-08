async function fetchManifest() {
  const res = await fetch("./manifest.json");
  if (!res.ok) throw new Error(`failed to load manifest.json: ${res.status}`);
  return res.json();
}

function formatDate(isoDate) {
  const d = new Date(`${isoDate}T00:00:00`);
  return d.toLocaleDateString(undefined, { year: "numeric", month: "long" });
}

function pickUrl(webpPath, avifPath) {
  const path = window.AVIF_OK && avifPath ? avifPath : webpPath;
  return `./${path}`;
}

function indexRow(href, nameText, metaText, title) {
  const li = document.createElement("li");
  const a = document.createElement("a");
  a.className = "index-row";
  a.href = href;

  const name = document.createElement("span");
  name.className = "index-name";
  name.textContent = nameText;
  if (title) name.title = title;

  const meta = document.createElement("span");
  meta.className = "label index-date";
  meta.textContent = metaText;

  a.appendChild(name);
  a.appendChild(meta);
  li.appendChild(a);
  return li;
}

function currentView() {
  return new URLSearchParams(location.search).get("view") === "tags" ? "tags" : "folders";
}

function renderFolders(manifest, container) {
  if (!manifest.albums.length) {
    container.innerHTML = '<li class="status">No albums yet.</li>';
    return;
  }
  for (const album of manifest.albums) {
    container.appendChild(
      indexRow(
        `./album.html?album=${encodeURIComponent(album.slug)}`,
        album.name,
        album.dateLabel || formatDate(album.date),
        album.description
      )
    );
  }
}

function renderTags(manifest, container) {
  const tags = manifest.tags || [];
  if (!tags.length) {
    container.innerHTML = '<li class="status">No tags yet.</li>';
    return;
  }
  for (const tag of tags) {
    container.appendChild(indexRow(`./album.html?tag=${encodeURIComponent(tag.slug)}`, tag.name, String(tag.count)));
  }
}

function renderHome(manifest) {
  const container = document.getElementById("albums");
  if (!container) return;

  const view = currentView();
  document.querySelectorAll(".view-link").forEach((link) => {
    const active = link.dataset.view === view;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });

  container.innerHTML = "";
  if (view === "tags") renderTags(manifest, container);
  else renderFolders(manifest, container);
}

function bindViewToggle(manifest) {
  // Switch views in place (no reload), but keep the URL in sync so reload,
  // back/forward and shared links all land on the same view.
  document.querySelectorAll(".view-link").forEach((link) => {
    link.addEventListener("click", (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
      e.preventDefault();
      if (link.dataset.view === currentView()) return;
      history.pushState(null, "", link.href);
      renderHome(manifest);
      window.scrollTo(0, 0);
    });
  });
  window.addEventListener("popstate", () => renderHome(manifest));
}

// Resolve ?album= or ?tag= into one shape the grid/lightbox can render:
// a tag view is just a synthetic album whose photos span every folder.
function resolveCollection(manifest) {
  const params = new URLSearchParams(location.search);
  const tagSlug = params.get("tag");
  if (tagSlug) {
    const tag = (manifest.tags || []).find((t) => t.slug === tagSlug);
    if (!tag) return null;
    const photos = [];
    for (const album of manifest.albums) {
      for (const photo of album.photos) {
        if ((photo.tags || []).includes(tag.name)) photos.push({ ...photo, albumName: album.name });
      }
    }
    return {
      kind: "tag",
      name: tag.name,
      subtitle: `${photos.length} ${photos.length === 1 ? "photo" : "photos"}`,
      photos,
    };
  }

  const album = manifest.albums.find((a) => a.slug === params.get("album"));
  if (!album) return null;
  return {
    kind: "album",
    name: album.name,
    subtitle: formatDate(album.date),
    photos: album.photos.map((p) => ({ ...p, albumName: album.name })),
  };
}

function renderAlbum(manifest) {
  const container = document.getElementById("photos");
  if (!container) return;

  const collection = resolveCollection(manifest);
  if (!collection) {
    container.innerHTML = '<p class="status">Not found.</p>';
    return;
  }

  const dataLayer = (window.dataLayer = window.dataLayer || []);
  if (collection.kind === "tag") {
    dataLayer.push({ event: "tag_view", tag_name: collection.name });
    const back = document.querySelector(".back-link");
    if (back) {
      back.href = "./index.html?view=tags";
      back.setAttribute("aria-label", "Back to tags");
    }
  } else {
    dataLayer.push({ event: "album_view", album_name: collection.name });
  }

  document.title = `img11.jpg - ${collection.name}`;
  const titleEl = document.getElementById("album-title");
  if (titleEl) titleEl.textContent = collection.name;
  const dateEl = document.getElementById("album-date");
  if (dateEl) dateEl.textContent = collection.subtitle;

  const photosForLightbox = collection.photos.map((p) => ({
    slug: p.slug,
    full: pickUrl(p.full, p.fullAvif),
    albumName: p.albumName,
    meta: p.meta,
  }));

  container.innerHTML = "";
  collection.photos.forEach((photo, i) => {
    const btn = document.createElement("button");
    btn.className = "photo-btn";
    btn.setAttribute("aria-label", `Open photo ${i + 1}`);

    const img = document.createElement("img");
    img.src = pickUrl(photo.thumb, photo.thumbAvif);
    img.loading = "lazy";
    img.width = photo.thumbWidth;
    img.height = photo.thumbHeight;
    img.alt = `${photo.albumName} photo ${i + 1}`;

    btn.appendChild(img);
    btn.addEventListener("click", () => Lightbox.open(photosForLightbox, i, collection.name));

    container.appendChild(btn);
  });
}

(async function main() {
  try {
    const [avifOk, manifest] = await Promise.all([AvifSupport.check(), fetchManifest()]);
    window.AVIF_OK = avifOk;
    if (document.getElementById("albums")) {
      renderHome(manifest);
      bindViewToggle(manifest);
    } else if (document.getElementById("photos")) {
      renderAlbum(manifest);
    }
  } catch (err) {
    console.error(err);
    const target = document.getElementById("albums") || document.getElementById("photos");
    if (target) target.innerHTML = `<p class="status">Failed to load photos: ${err.message}</p>`;
  }
})();
