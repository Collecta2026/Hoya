/* Lightweight drawn-signature pad (mouse + touch), no external library.
 * Usage: initSignaturePad({ canvasId, hiddenInputId, clearBtnId })
 * Writes a base64 PNG data URL into the hidden input as the user draws,
 * and on clear. */
function initSignaturePad(opts) {
  var canvas = document.getElementById(opts.canvasId);
  if (!canvas) return;
  var hidden = document.getElementById(opts.hiddenInputId);
  var clearBtn = opts.clearBtnId ? document.getElementById(opts.clearBtnId) : null;
  var ctx = canvas.getContext("2d");

  function resize() {
    var ratio = window.devicePixelRatio || 1;
    var rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * ratio;
    canvas.height = rect.height * ratio;
    ctx.scale(ratio, ratio);
    ctx.lineWidth = 2;
    ctx.lineCap = "round";
    ctx.strokeStyle = "#111";
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, rect.width, rect.height);
  }
  resize();

  var drawing = false;
  var hasSigned = false;

  function pos(e) {
    var rect = canvas.getBoundingClientRect();
    var x, y;
    if (e.touches && e.touches.length) {
      x = e.touches[0].clientX - rect.left;
      y = e.touches[0].clientY - rect.top;
    } else {
      x = e.clientX - rect.left;
      y = e.clientY - rect.top;
    }
    return { x: x, y: y };
  }

  function start(e) {
    e.preventDefault();
    drawing = true;
    hasSigned = true;
    var p = pos(e);
    ctx.beginPath();
    ctx.moveTo(p.x, p.y);
  }
  function move(e) {
    if (!drawing) return;
    e.preventDefault();
    var p = pos(e);
    ctx.lineTo(p.x, p.y);
    ctx.stroke();
  }
  function end() {
    if (!drawing) return;
    drawing = false;
    if (hidden) hidden.value = canvas.toDataURL("image/png");
  }

  canvas.addEventListener("mousedown", start);
  canvas.addEventListener("mousemove", move);
  window.addEventListener("mouseup", end);
  canvas.addEventListener("touchstart", start, { passive: false });
  canvas.addEventListener("touchmove", move, { passive: false });
  canvas.addEventListener("touchend", end);

  if (clearBtn) {
    clearBtn.addEventListener("click", function (e) {
      e.preventDefault();
      var rect = canvas.getBoundingClientRect();
      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, rect.width, rect.height);
      hasSigned = false;
      if (hidden) hidden.value = "";
    });
  }

  window.addEventListener("resize", resize);
}
