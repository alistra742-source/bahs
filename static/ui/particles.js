/* Falling white particles on a black stage. Decorative only; off for reduced motion. */
(function () {
  'use strict';
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  const canvas = document.createElement('canvas');
  canvas.id = 'bg-particles';
  canvas.setAttribute('aria-hidden', 'true');
  document.body.prepend(canvas);
  const ctx = canvas.getContext('2d');

  let width = 0, height = 0, dpr = 1, particles = [];

  function spawn(p, initial) {
    p.x = Math.random() * width;
    p.y = initial ? Math.random() * height : -6;
    p.r = 0.4 + Math.random() * 1.4;
    p.vy = 0.2 + Math.random() * 0.7;
    p.vx = (Math.random() - 0.5) * 0.15;
    p.a = 0.25 + Math.random() * 0.6;
    return p;
  }

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const target = Math.min(160, Math.max(40, Math.floor((width * height) / 12000)));
    while (particles.length < target) particles.push(spawn({}, true));
    particles.length = target;
  }

  function frame() {
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = '#ffffff';
    for (const p of particles) {
      p.y += p.vy;
      p.x += p.vx;
      if (p.y > height + 6) spawn(p, false);
      ctx.globalAlpha = p.a;
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
    requestAnimationFrame(frame);
  }

  window.addEventListener('resize', resize);
  resize();
  requestAnimationFrame(frame);
})();
