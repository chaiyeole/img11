async function fetchManifest() {
  const res = await fetch("./manifest.json");
  if (!res.ok) throw new Error(`failed to load manifest.json: ${res.status}`);
  return res.json();
}

function formatDate(isoDate) {
  const d = new Date(`${isoDate}T00:00:00`);
  return d.toLocaleDateString(undefined, { year: "numeric", month: "long" });
}

function renderHome(manifest) {
  const container = document.getElementById("albums");
  if (!container) return;

  if (!manifest.albums.length) {
    container.innerHTML = '<li class="status">No albums yet.</li>';
    return;
  }

  container.innerHTML = "";
  for (const album of manifest.albums) {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.className = "index-row";
    a.href = `./album.html?album=${encodeURIComponent(album.slug)}`;

    const name = document.createElement("span");
    name.className = "index-name";
    name.textContent = album.name;

    const date = document.createElement("span");
    date.className = "label index-date";
    date.textContent = formatDate(album.date);

    a.appendChild(name);
    a.appendChild(date);
    li.appendChild(a);
    container.appendChild(li);
  }
}

function renderAlbum(manifest) {
  const container = document.getElementById("photos");
  if (!container) return;

  const slug = new URLSearchParams(location.search).get("album");
  const album = manifest.albums.find((a) => a.slug === slug);

  if (!album) {
    container.innerHTML = '<p class="status">Album not found.</p>';
    return;
  }

  document.title = `${album.name} – Photo Portfolio`;
  const titleEl = document.getElementById("album-title");
  if (titleEl) titleEl.textContent = album.name;
  const dateEl = document.getElementById("album-date");
  if (dateEl) dateEl.textContent = formatDate(album.date);

  container.innerHTML = "";
  album.photos.forEach((photo, i) => {
    const btn = document.createElement("button");
    btn.className = "photo-btn";
    btn.setAttribute("aria-label", `Open photo ${i + 1}`);

    const img = document.createElement("img");
    img.src = `./${photo.thumb}`;
    img.loading = "lazy";
    img.width = photo.thumbWidth;
    img.height = photo.thumbHeight;
    img.alt = `${album.name} photo ${i + 1}`;

    btn.appendChild(img);
    btn.addEventListener("click", () => {
      const photosForLightbox = album.photos.map((p) => ({ slug: p.slug, full: `./${p.full}` }));
      Lightbox.open(photosForLightbox, i, album.name);
    });

    container.appendChild(btn);
  });
}

(async function main() {
  try {
    const manifest = await fetchManifest();
    if (document.getElementById("albums")) {
      renderHome(manifest);
    } else if (document.getElementById("photos")) {
      renderAlbum(manifest);
    }
  } catch (err) {
    console.error(err);
    const target = document.getElementById("albums") || document.getElementById("photos");
    if (target) target.innerHTML = `<p class="status">Failed to load photos: ${err.message}</p>`;
  }
})();
