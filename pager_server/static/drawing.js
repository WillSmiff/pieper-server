// Shared helpers for 1-bit pager drawings (used by the send page and the admin page).
// Format: rows top to bottom, each row packed MSB-first and padded to whole bytes.
window.PagerDrawing = (() => {
  const INK = [17, 24, 39];        // dark ink…
  const PAPER = [255, 255, 255];   // …on white, like pen on paper

  function stride(width) { return (width + 7) >> 3; }

  // One byte per pixel (0/1)  ->  base64 of the packed bitmap.
  function encode(pixels, width, height) {
    const s = stride(width);
    const packed = new Uint8Array(s * height);
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        if (pixels[y * width + x]) packed[y * s + (x >> 3)] |= 0x80 >> (x & 7);
      }
    }
    let bin = "";
    for (let i = 0; i < packed.length; i += 0x8000) {
      bin += String.fromCharCode.apply(null, packed.subarray(i, i + 0x8000));
    }
    return btoa(bin);
  }

  // base64 packed bitmap  ->  one byte per pixel (0/1).
  function decode(b64, width, height) {
    const bin = atob(b64);
    const s = stride(width);
    const pixels = new Uint8Array(width * height);
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        pixels[y * width + x] = (bin.charCodeAt(y * s + (x >> 3)) >> (7 - (x & 7))) & 1;
      }
    }
    return pixels;
  }

  // Paint pixels onto a canvas whose width/height attributes match the drawing.
  function paint(canvas, pixels, width, height) {
    const ctx = canvas.getContext("2d");
    const img = ctx.createImageData(width, height);
    for (let i = 0; i < pixels.length; i++) {
      const c = pixels[i] ? INK : PAPER;
      img.data[i * 4] = c[0]; img.data[i * 4 + 1] = c[1]; img.data[i * 4 + 2] = c[2];
      img.data[i * 4 + 3] = 255;
    }
    ctx.putImageData(img, 0, 0);
  }

  // Thumbnail canvas for a sent page; images are fetched once and cached.
  const cache = new Map();
  function thumbnail(messageId) {
    const canvas = document.createElement("canvas");
    canvas.className = "thumb";
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", "Drawing");
    let entry = cache.get(messageId);
    if (!entry) {
      entry = fetch(`/api/messages/${messageId}/image`)
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))))
        .then((d) => ({ width: d.width, height: d.height, pixels: decode(d.data, d.width, d.height) }));
      entry.catch(() => cache.delete(messageId));
      cache.set(messageId, entry);
    }
    entry.then((d) => {
      canvas.width = d.width;
      canvas.height = d.height;
      paint(canvas, d.pixels, d.width, d.height);
    }).catch(() => { /* leave blank */ });
    return canvas;
  }

  return { encode, decode, paint, thumbnail };
})();
