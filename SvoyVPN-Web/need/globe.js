/**
 * SvoyVPN — dot globe (Three.js)
 * Full-axis drag (X+Y), inertia, touch/mobile support
 */
import * as THREE from 'three';

const ACCENT       = 0x00ff85;
const ACCENT_COLOR = new THREE.Color(ACCENT);
const R            = 1;
const ATMOS_R      = R * 1.06;
const MAX_PITCH    = Math.PI * 0.48; // don't flip over the poles completely

const SERVERS = [
  { id: 'ams', name: 'Амстердам',  region: 'EU-West',    lat: 52.37, lon:   4.90, latency: 14, uptime: 99.99, load: 24 },
  { id: 'fra', name: 'Франкфурт', region: 'EU-Central', lat: 50.11, lon:   8.68, latency: 16, uptime: 99.98, load: 31 },
  { id: 'hel', name: 'Хельсинки', region: 'Nordic',     lat: 60.17, lon:  24.94, latency: 19, uptime: 99.97, load: 18 },
  { id: 'lon', name: 'Лондон',    region: 'UK',         lat: 51.51, lon:  -0.13, latency: 17, uptime: 99.99, load: 27 },
  { id: 'sgp', name: 'Сингапур',  region: 'APAC',       lat:  1.35, lon: 103.82, latency: 42, uptime: 99.96, load: 35 },
  { id: 'tky', name: 'Токио',     region: 'APAC',       lat: 35.68, lon: 139.69, latency: 48, uptime: 99.95, load: 29 },
  { id: 'nyc', name: 'Нью-Йорк', region: 'US-East',    lat: 40.71, lon: -74.01, latency: 38, uptime: 99.97, load: 33 },
];

function ll2v(lat, lon, r) {
  const φ = THREE.MathUtils.degToRad(90 - lat);
  const θ = THREE.MathUtils.degToRad(lon + 180);
  return new THREE.Vector3(
    -r * Math.sin(φ) * Math.cos(θ),
     r * Math.cos(φ),
     r * Math.sin(φ) * Math.sin(θ),
  );
}

/* ---------- land dots -------------------------------------------------- */
async function buildLandDots(radius) {
  const img = new Image();
  img.crossOrigin = 'anonymous';
  await new Promise((res, rej) => {
    img.onload = res;
    img.onerror = rej;
    img.src = 'https://cdn.jsdelivr.net/gh/mrdoob/three.js@r160/examples/textures/planets/earth_atmos_2048.jpg';
  });

  const W = 1024, H = 512;
  const offscreen = document.createElement('canvas');
  offscreen.width = W; offscreen.height = H;
  const ctx = offscreen.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0, W, H);
  const data = ctx.getImageData(0, 0, W, H).data;

  function isLand(lat, lon) {
    const x = Math.round(((lon + 180) / 360) * (W - 1));
    const y = Math.round(((90 - lat) / 180) * (H - 1));
    let land = 0;
    for (let dy = -1; dy <= 1; dy++) {
      for (let dx = -1; dx <= 1; dx++) {
        const px = Math.min(W - 1, Math.max(0, x + dx));
        const py = Math.min(H - 1, Math.max(0, y + dy));
        const i  = (py * W + px) * 4;
        const r = data[i], g = data[i + 1], b = data[i + 2];
        if (!(b > r + 15 && b > g + 5 && b > 40)) land++;
      }
    }
    return land >= 5;
  }

  const positions = [], colors = [];
  const step = 2.2;

  for (let lat = -85; lat <= 85; lat += step) {
    const cosLat  = Math.cos(THREE.MathUtils.degToRad(lat));
    const lonStep = step / Math.max(cosLat, 0.3);
    for (let lon = -180; lon < 180; lon += lonStep) {
      if (!isLand(lat, lon)) continue;
      const v = ll2v(lat, lon, radius);
      positions.push(v.x, v.y, v.z);
      const s = 0.78 + (Math.abs(lat) / 85) * 0.12;
      colors.push(ACCENT_COLOR.r * s, ACCENT_COLOR.g * s, ACCENT_COLOR.b * s);
    }
  }

  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  geo.setAttribute('color',    new THREE.Float32BufferAttribute(colors, 3));
  return geo;
}

/* ---------- atmosphere rim --------------------------------------------- */
function makeAtmos() {
  return new THREE.Mesh(
    new THREE.SphereGeometry(ATMOS_R * 1.012, 48, 48),
    new THREE.ShaderMaterial({
      uniforms: { c: { value: new THREE.Color(ACCENT) } },
      vertexShader: `
        varying vec3 vN;
        void main(){
          vN = normalize(normalMatrix * normal);
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0);
        }
      `,
      fragmentShader: `
        uniform vec3 c;
        varying vec3 vN;
        void main(){
          float i = pow(max(0.0, 0.28 - dot(vN, vec3(0,0,1))), 5.0);
          gl_FragColor = vec4(c, i * 0.6);
        }
      `,
      side: THREE.BackSide,
      blending: THREE.AdditiveBlending,
      transparent: true,
      depthWrite: false,
    }),
  );
}

/* ---------- arc -------------------------------------------------------- */
function makeArc(a, b) {
  const s = ll2v(a.lat, a.lon, R * 1.01);
  const e = ll2v(b.lat, b.lon, R * 1.01);
  const m = s.clone().add(e).normalize().multiplyScalar(R * 1.14);
  const pts = new THREE.QuadraticBezierCurve3(s, m, e).getPoints(40);
  return new THREE.Line(
    new THREE.BufferGeometry().setFromPoints(pts),
    new THREE.LineBasicMaterial({ color: ACCENT, transparent: true, opacity: 0.09 }),
  );
}

/* ======================================================================= */
export async function initGlobe(container) {
  if (!container) return;

  const panel = document.getElementById('globe-panel');
  let canvas  = container.querySelector('canvas');
  if (!canvas) { canvas = document.createElement('canvas'); container.appendChild(canvas); }

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.setClearColor(0x000000, 0);

  const scene  = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(40, 1, 0.1, 40);

  /* globe group — no initial rotation; we manage pitch/yaw ourselves */
  const globe = new THREE.Group();
  scene.add(globe);

  /* ocean sphere */
  globe.add(new THREE.Mesh(
    new THREE.SphereGeometry(R * 0.998, 48, 48),
    new THREE.MeshBasicMaterial({ color: 0x060d0a }),
  ));

  /* land dots */
  const dotGeo = await buildLandDots(R);
  const dotMat = new THREE.PointsMaterial({
    size: 0.013, vertexColors: true,
    transparent: true, opacity: 0.93,
    sizeAttenuation: true, depthWrite: false,
  });
  globe.add(new THREE.Points(dotGeo, dotMat));

  /* atmosphere */
  globe.add(makeAtmos());

  /* server nodes */
  const hitMeshes = [];
  SERVERS.forEach((srv) => {
    const pos = ll2v(srv.lat, srv.lon, R * 1.008);
    const g   = new THREE.Group();
    g.position.copy(pos);
    g.lookAt(pos.clone().multiplyScalar(2));

    const core = new THREE.Mesh(
      new THREE.SphereGeometry(0.018, 12, 12),
      new THREE.MeshBasicMaterial({ color: ACCENT }),
    );
    const ring = new THREE.Mesh(
      new THREE.SphereGeometry(0.036, 12, 12),
      new THREE.MeshBasicMaterial({ color: ACCENT, transparent: true, opacity: 0.2 }),
    );
    const hit = new THREE.Mesh(
      new THREE.SphereGeometry(0.06, 8, 8),
      new THREE.MeshBasicMaterial({ visible: false }),
    );
    hit.userData = { server: srv, core, ring };
    g.add(ring, core, hit);
    globe.add(g);
    hitMeshes.push(hit);
  });

  /* arcs */
  const hub = SERVERS[1];
  SERVERS.forEach((s) => { if (s.id !== hub.id) globe.add(makeArc(hub, s)); });

  /* ---------- panel --------------------------------------------------- */
  function showPanel(srv) {
    if (!panel) return;
    panel.hidden = false;
    panel.classList.add('visible');
    panel.querySelector('[data-g-name]').textContent    = srv.name;
    panel.querySelector('[data-g-region]').textContent  = srv.region;
    panel.querySelector('[data-g-latency]').textContent = srv.latency + ' ms';
    panel.querySelector('[data-g-uptime]').textContent  = srv.uptime  + '%';
    panel.querySelector('[data-g-load]').textContent    = srv.load    + '%';
  }
  function hidePanel() {
    if (!panel) return;
    panel.classList.remove('visible');
    setTimeout(() => { if (!panel.classList.contains('visible')) panel.hidden = true; }, 300);
  }
  if (!panel?.dataset.bound) {
    panel?.querySelector('[data-g-close]')?.addEventListener('click', hidePanel);
    if (panel) panel.dataset.bound = '1';
  }

  /* ---------- camera fit ---------------------------------------------- */
  function resize() {
    const w = container.clientWidth, h = container.clientHeight;
    if (w < 8 || h < 8) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    const vHalf = THREE.MathUtils.degToRad(camera.fov) / 2;
    const hHalf = Math.atan(Math.tan(vHalf) * camera.aspect);
    camera.position.z = (ATMOS_R / Math.sin(Math.min(vHalf, hHalf))) * 1.18;
    camera.updateProjectionMatrix();
  }
  resize();
  const ro = new ResizeObserver(resize);
  ro.observe(container);

  /* ---------- rotation state ------------------------------------------ */
  let yaw       = 0;      // radians around Y
  let pitch     = 0.12;   // radians around X (slight initial tilt)
  let velYaw    = 0;
  let velPitch  = 0;
  let autoSpin  = true;
  let isDragging = false;

  /* ---------- raycaster ------------------------------------------------ */
  const rc  = new THREE.Raycaster();
  const ptr = new THREE.Vector2();
  function pick(cx, cy) {
    const rect = canvas.getBoundingClientRect();
    ptr.x =  ((cx - rect.left) / rect.width)  * 2 - 1;
    ptr.y = -((cy - rect.top)  / rect.height) * 2 + 1;
    rc.setFromCamera(ptr, camera);
    const hits = rc.intersectObjects(hitMeshes, false);
    return hits.length ? hits[0].object : null;
  }

  /* ---------- pointer / touch helpers --------------------------------- */
  // Track active touches for multi-touch (pinch could be added later)
  let lastPX = 0, lastPY = 0;
  let downPX = 0, downPY = 0;  // for tap detection

  function onDown(cx, cy) {
    isDragging = true;
    autoSpin   = false;
    velYaw     = 0;
    velPitch   = 0;
    lastPX = downPX = cx;
    lastPY = downPY = cy;
  }

  function onMove(cx, cy) {
    if (!isDragging) return;
    const dx = cx - lastPX;
    const dy = cy - lastPY;
    const speed = 0.005;
    velYaw   = dx * speed;
    velPitch = dy * speed;
    yaw   += velYaw;
    pitch += velPitch;
    pitch  = Math.max(-MAX_PITCH, Math.min(MAX_PITCH, pitch));
    lastPX = cx;
    lastPY = cy;
  }

  function onUp(cx, cy, isTap) {
    if (!isDragging) return;
    isDragging = false;
    if (isTap) {
      const h = pick(cx, cy);
      if (h?.userData?.server) showPanel(h.userData.server);
      else hidePanel();
    }
  }

  /* ---------- pointer events (mouse + touch via pointer API) ---------- */
  canvas.addEventListener('pointerdown', (e) => {
    canvas.setPointerCapture(e.pointerId);
    onDown(e.clientX, e.clientY);
  });

  canvas.addEventListener('pointermove', (e) => {
    if (isDragging) {
      onMove(e.clientX, e.clientY);
      canvas.style.cursor = 'grabbing';
    } else {
      const h = pick(e.clientX, e.clientY);
      canvas.style.cursor = h ? 'pointer' : 'grab';
      hitMeshes.forEach((m) => {
        const on = m === h;
        m.userData.core.scale.setScalar(on ? 1.5 : 1);
        m.userData.ring.material.opacity = on ? 0.45 : 0.2;
      });
    }
  });

  canvas.addEventListener('pointerup', (e) => {
    canvas.releasePointerCapture(e.pointerId);
    const dx = Math.abs(e.clientX - downPX);
    const dy = Math.abs(e.clientY - downPY);
    const isTap = dx < 8 && dy < 8;
    onUp(e.clientX, e.clientY, isTap);
    canvas.style.cursor = 'grab';
  });

  canvas.addEventListener('pointercancel', () => {
    isDragging = false;
    canvas.style.cursor = 'grab';
  });

  /* Touch events for Safari (pointer events may not fire for touch in some Safari versions) */
  canvas.addEventListener('touchstart', (e) => {
    e.preventDefault();
    const t = e.touches[0];
    onDown(t.clientX, t.clientY);
  }, { passive: false });

  canvas.addEventListener('touchmove', (e) => {
    e.preventDefault();
    const t = e.touches[0];
    onMove(t.clientX, t.clientY);
  }, { passive: false });

  canvas.addEventListener('touchend', (e) => {
    e.preventDefault();
    const t = e.changedTouches[0];
    const dx = Math.abs(t.clientX - downPX);
    const dy = Math.abs(t.clientY - downPY);
    onUp(t.clientX, t.clientY, dx < 10 && dy < 10);
  }, { passive: false });

  /* ---------- visibility --------------------------------------------- */
  let inView = true;
  const io = new IntersectionObserver(
    (entries) => { inView = entries[0]?.isIntersecting ?? true; },
    { threshold: 0.05 },
  );
  io.observe(container);

  /* ---------- render loop -------------------------------------------- */
  const DAMPING   = 0.88;   // inertia decay per frame
  const AUTO_SPEED = 0.0008;
  const clk = new THREE.Clock();
  let raf;

  function tick() {
    raf = requestAnimationFrame(tick);
    if (!inView) return;
    const t = clk.getElapsedTime();

    if (isDragging) {
      // nothing extra — updated in onMove
    } else {
      // inertia
      yaw   += velYaw;
      pitch += velPitch;
      pitch  = Math.max(-MAX_PITCH, Math.min(MAX_PITCH, pitch));
      velYaw   *= DAMPING;
      velPitch *= DAMPING;

      // resume auto-spin when inertia dies
      if (Math.abs(velYaw) < 0.0001 && Math.abs(velPitch) < 0.0001) {
        if (!autoSpin) {
          // gentle resume
          velYaw = 0; velPitch = 0;
          autoSpin = true;
        }
      }
      if (autoSpin) yaw += AUTO_SPEED;
    }

    globe.rotation.order = 'YXZ';
    globe.rotation.y = yaw;
    globe.rotation.x = pitch;

    // pulse server rings
    hitMeshes.forEach((m, i) => {
      m.userData.ring.scale.setScalar(1 + Math.sin(t * 1.8 + i) * 0.28);
    });

    renderer.render(scene, camera);
  }

  tick();
  showPanel(SERVERS[1]);

  return () => {
    cancelAnimationFrame(raf);
    ro.disconnect();
    io.disconnect();
    renderer.dispose();
  };
}
