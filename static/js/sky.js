/*
 * Nuage Libre: living backgrounds.
 *
 *   Day theme   -> a few soft clouds drifting across the dawn sky.
 *                  Made of plain <div>s animated by CSS (css/app.css,
 *                  .drift-cloud); this script only creates them with random
 *                  sizes, heights and speeds so the sky never looks repeated.
 *
 *   Night theme -> a star field drawn on a <canvas>:
 *                  - each star twinkles at its own rhythm,
 *                  - stars sit at different "depths": when the mouse moves,
 *                    near stars move more than far ones = parallax effect,
 *                  - now and then, a shooting star crosses the sky.
 *
 *   Pro theme   -> nothing moves (handled by CSS alone).
 *
 * Why <canvas> for the stars and not hundreds of <div>s? Animating
 * hundreds of DOM elements forces the browser to manage hundreds of
 * layers; a canvas is a single bitmap we redraw ourselves, which is far
 * cheaper.
 *
 * Eco-design and battery: the animation loop runs ONLY while the night
 * theme is active and the tab is visible. With "reduced motion" requested
 * by the OS, the sky is drawn once, without twinkling, parallax or shooting
 * stars.
 *
 * No library is used: requestAnimationFrame + Canvas 2D are enough.
 */
(() => {
  'use strict';

  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const root = document.documentElement;

  // ====================================================== drifting clouds
  function createDriftClouds() {
    const container = document.getElementById('drift-clouds');
    if (!container) return;
    const COUNT = 7;
    for (let i = 0; i < COUNT; i += 1) {
      const cloud = document.createElement('div');
      cloud.className = 'drift-cloud';
      // Far clouds (small) are slower, blurrier and paler: depth cue.
      const depth = Math.random();                      // 0 = far, 1 = near
      const duration = 160 - depth * 90;                // 70 s .. 160 s
      cloud.style.setProperty('--y', `${4 + Math.random() * 78}%`);
      cloud.style.setProperty('--scale', (0.45 + depth * 0.9).toFixed(2));
      cloud.style.setProperty('--opacity', (0.35 + depth * 0.45).toFixed(2));
      cloud.style.setProperty('--blur', `${(3 - depth * 2.4).toFixed(1)}px`);
      cloud.style.setProperty('--duration', `${duration.toFixed(0)}s`);
      // Negative delay: the cloud starts mid-way, so the sky is already
      // populated when the page opens (instead of empty for a minute).
      cloud.style.setProperty('--delay', `${(-Math.random() * duration).toFixed(0)}s`);
      container.appendChild(cloud);
    }
  }

  // ========================================================== star field
  const canvas = document.getElementById('star-canvas');
  const ctx = canvas ? canvas.getContext('2d') : null;

  let stars = [];
  let shootingStars = [];
  let width = 0;
  let height = 0;
  let frameId = null;
  let nextShootingStarAt = 0;

  // Parallax: `target` follows the mouse instantly, `offset` moves towards
  // it a little each frame (smoothing), which gives a floaty, inertial feel.
  const pointer = { targetX: 0, targetY: 0, x: 0, y: 0 };
  const PARALLAX_PX = 22;   // max shift of the nearest stars, in pixels

  // A few stars are tinted with the brand colours.
  const STAR_COLORS = ['255,255,255', '255,255,255', '255,255,255', '200,236,254', '252,214,223'];

  function resizeCanvas() {
    // devicePixelRatio: on "retina" screens one CSS pixel = 2+ physical
    // pixels. Drawing at the physical resolution keeps stars sharp. Capped
    // at 2 to limit the cost on very dense screens.
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    createStars();
  }

  function createStars() {
    // Density proportional to the screen area, with a ceiling.
    const count = Math.min(Math.round((width * height) / 2400), 650);
    stars = Array.from({ length: count }, () => {
      const depth = Math.random() ** 2;   // squared: most stars are far away
      return {
        x: Math.random(),                 // position as a fraction of the screen
        y: Math.random(),
        depth,
        radius: 0.35 + depth * 1.35,
        alpha: 0.35 + Math.random() * 0.55,
        twinkleSpeed: 0.6 + Math.random() * 2.2,
        phase: Math.random() * Math.PI * 2,
        color: STAR_COLORS[Math.floor(Math.random() * STAR_COLORS.length)],
      };
    });
  }

  function spawnShootingStar(now) {
    const angle = (Math.PI / 180) * (20 + Math.random() * 25);  // 20°..45° downwards
    const speed = 0.9 + Math.random() * 0.6;                     // px per ms
    shootingStars.push({
      x: Math.random() * width * 0.8,
      y: Math.random() * height * 0.35,
      vx: Math.cos(angle) * speed,
      vy: Math.sin(angle) * speed,
      born: now,
      life: 900 + Math.random() * 500,                           // ms
    });
    nextShootingStarAt = now + 5000 + Math.random() * 9000;      // every 5-14 s
  }

  let lastTime = 0;

  function draw(now) {
    const dt = lastTime ? Math.min(now - lastTime, 50) : 16;
    lastTime = now;
    const animate = !reducedMotion.matches;

    // Smooth the parallax offset towards the pointer target (6 % per frame).
    pointer.x += (pointer.targetX - pointer.x) * 0.06;
    pointer.y += (pointer.targetY - pointer.y) * 0.06;

    ctx.clearRect(0, 0, width, height);

    for (const s of stars) {
      // Twinkle: brightness oscillates around its base value (sine wave).
      const twinkle = animate ? 0.65 + 0.35 * Math.sin(now * 0.001 * s.twinkleSpeed + s.phase) : 1;
      const x = s.x * width + pointer.x * PARALLAX_PX * s.depth;
      const y = s.y * height + pointer.y * PARALLAX_PX * s.depth;

      ctx.globalAlpha = s.alpha * twinkle;
      ctx.fillStyle = `rgb(${s.color})`;
      ctx.beginPath();
      ctx.arc(x, y, s.radius, 0, Math.PI * 2);
      ctx.fill();

      // The brightest, nearest stars get a soft halo (glow).
      if (s.depth > 0.7) {
        ctx.globalAlpha = s.alpha * twinkle * 0.18;
        ctx.beginPath();
        ctx.arc(x, y, s.radius * 4, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    if (animate) {
      if (now > nextShootingStarAt) spawnShootingStar(now);
      shootingStars = shootingStars.filter((star) => now - star.born < star.life);
      for (const star of shootingStars) {
        const progress = (now - star.born) / star.life;          // 0 -> 1
        star.x += star.vx * dt;
        star.y += star.vy * dt;
        // Tail = a line pointing backwards, fading from head to tail.
        const tailX = star.x - star.vx * 140;
        const tailY = star.y - star.vy * 140;
        const gradient = ctx.createLinearGradient(star.x, star.y, tailX, tailY);
        gradient.addColorStop(0, 'rgba(255,255,255,0.95)');
        gradient.addColorStop(0.3, 'rgba(91,206,250,0.45)');
        gradient.addColorStop(1, 'rgba(245,169,184,0)');
        // Fade in during the first 15 %, fade out at the end.
        ctx.globalAlpha = Math.min(1, progress / 0.15) * (1 - progress);
        ctx.strokeStyle = gradient;
        ctx.lineWidth = 1.6;
        ctx.lineCap = 'round';
        ctx.beginPath();
        ctx.moveTo(star.x, star.y);
        ctx.lineTo(tailX, tailY);
        ctx.stroke();
      }
    }

    ctx.globalAlpha = 1;
    // Keep looping only if something actually moves.
    frameId = animate ? requestAnimationFrame(draw) : null;
  }

  const isNight = () => root.dataset.theme === 'night';

  function start() {
    if (!ctx || frameId !== null || !isNight() || document.hidden) return;
    lastTime = 0;
    nextShootingStarAt = performance.now() + 2500;  // first one soon after opening
    frameId = requestAnimationFrame(draw);
  }

  function stop() {
    if (frameId !== null) cancelAnimationFrame(frameId);
    frameId = null;
  }

  function restart() {
    stop();
    start();
  }

  function initStars() {
    if (!ctx) return;
    resizeCanvas();

    let resizeTimer = null;
    window.addEventListener('resize', () => {
      // Debounce: rebuild the stars once, after the user stops resizing.
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        resizeCanvas();
        restart();
      }, 150);
    });

    // Pointer position mapped to -1..1 around the screen centre. The stars
    // move in the opposite direction of the mouse, like a distant landscape
    // seen from a moving window.
    window.addEventListener('pointermove', (event) => {
      pointer.targetX = -((event.clientX / width) * 2 - 1);
      pointer.targetY = -((event.clientY / height) * 2 - 1);
    }, { passive: true });

    // Pause when the tab is hidden: no CPU/GPU spent on an invisible sky.
    document.addEventListener('visibilitychange', () => (document.hidden ? stop() : start()));
    document.addEventListener('themechange', () => (isNight() ? start() : stop()));
    reducedMotion.addEventListener('change', restart);

    start();
  }

  createDriftClouds();
  initStars();
})();
