/**
 * The provider pool, as a thing you can watch.
 *
 * The dashboard already listed providers in a table. A table is fine for
 * "what is configured" and poor for the question people actually have while a
 * task is running: *where is the work right now, and what just fell over?*
 *
 * So: one node per provider on a ring, the orchestrator at the centre, and a
 * pulse travelling out along a link whenever that provider is executing. Health
 * is colour, load is size. Nothing here is decorative — every visual property
 * is bound to a field the API already returns, and if the data stops arriving
 * the scene stops moving rather than looping a pleasant lie.
 *
 * three.js rather than SVG because this animates continuously against live
 * state; SVG is the right tool for the static README hero, and the wrong one
 * for sixty frames a second of changing data.
 */

import { useEffect, useRef } from "react";
import * as THREE from "three";

export type ProviderNode = {
  id: string;
  available: boolean;
  /** Subtasks executing on this provider right now. */
  inflight?: number;
  /** DISABLED means a terminal failure — out of rotation until an operator acts. */
  disabled?: boolean;
};

const COLOR_OK = 0x3fb950;
const COLOR_BUSY = 0x58a6ff;
const COLOR_DOWN = 0xf85149;
const COLOR_DISABLED = 0x6e7681;
const COLOR_LINK = 0x30363d;

function colorFor(p: ProviderNode): number {
  if (p.disabled) return COLOR_DISABLED;
  if (!p.available) return COLOR_DOWN;
  return (p.inflight ?? 0) > 0 ? COLOR_BUSY : COLOR_OK;
}

export default function ProviderGlobe({
  providers,
  height = 260,
}: {
  providers: ProviderNode[];
  height?: number;
}) {
  const mount = useRef<HTMLDivElement | null>(null);
  // The renderer must outlive React's render cycle; the latest providers are
  // read from a ref so the animation loop is never rebuilt on data changes.
  const latest = useRef(providers);
  latest.current = providers;

  useEffect(() => {
    const el = mount.current;
    if (!el) return;

    // Respect the user's motion preference. An always-spinning scene is a
    // vestibular problem for some people, and this one carries no information
    // that requires rotation to read.
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(45, el.clientWidth / height, 0.1, 100);
    camera.position.set(0, 3.2, 8);
    camera.lookAt(0, 0, 0);

    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    } catch {
      // No WebGL (headless, locked-down browser, some VMs). The table above is
      // the source of truth; this view is an addition, so failing to create it
      // must never take the dashboard down with it.
      return;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(el.clientWidth, height);
    el.appendChild(renderer.domElement);

    const root = new THREE.Group();
    scene.add(root);

    // The orchestrator: the thing that is actually in charge.
    const core = new THREE.Mesh(
      new THREE.IcosahedronGeometry(0.55, 1),
      new THREE.MeshBasicMaterial({ color: 0x818cf8, wireframe: true }),
    );
    root.add(core);

    const nodes: {
      mesh: THREE.Mesh;
      pulse: THREE.Mesh;
      material: THREE.MeshBasicMaterial;
      angle: number;
    }[] = [];
    const RADIUS = 3.4;

    latest.current.forEach((p, i) => {
      const angle = (i / Math.max(latest.current.length, 1)) * Math.PI * 2;
      const x = Math.cos(angle) * RADIUS;
      const z = Math.sin(angle) * RADIUS;

      const material = new THREE.MeshBasicMaterial({ color: colorFor(p) });
      const mesh = new THREE.Mesh(new THREE.SphereGeometry(0.26, 20, 20), material);
      mesh.position.set(x, 0, z);
      root.add(mesh);

      const link = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints([
          new THREE.Vector3(0, 0, 0),
          new THREE.Vector3(x, 0, z),
        ]),
        new THREE.LineBasicMaterial({ color: COLOR_LINK }),
      );
      root.add(link);

      // Travels the link while this provider is executing. Hidden otherwise —
      // motion means work is happening, so idle must be still.
      const pulse = new THREE.Mesh(
        new THREE.SphereGeometry(0.09, 10, 10),
        new THREE.MeshBasicMaterial({ color: COLOR_BUSY }),
      );
      pulse.visible = false;
      root.add(pulse);

      nodes.push({ mesh, pulse, material, angle });
    });

    let frame = 0;
    let t = 0;
    const clock = new THREE.Clock();

    const tick = () => {
      frame = requestAnimationFrame(tick);
      const dt = clock.getDelta();
      t += dt;

      if (!reduced) root.rotation.y += dt * 0.12;
      core.rotation.x += dt * 0.4;
      core.rotation.y += dt * 0.25;

      nodes.forEach((n, i) => {
        const p = latest.current[i];
        if (!p) return;
        n.material.color.setHex(colorFor(p));

        const busy = (p.inflight ?? 0) > 0;
        // Size carries load, so a provider holding four subtasks reads as
        // heavier than one holding a single subtask.
        const target = 0.26 + Math.min(p.inflight ?? 0, 4) * 0.05;
        n.mesh.scale.setScalar(THREE.MathUtils.lerp(n.mesh.scale.x, target / 0.26, 0.1));

        n.pulse.visible = busy && !reduced;
        if (busy && !reduced) {
          const k = (t * 0.8 + i * 0.3) % 1;
          n.pulse.position.set(
            Math.cos(n.angle) * RADIUS * k,
            0,
            Math.sin(n.angle) * RADIUS * k,
          );
        }
      });

      renderer.render(scene, camera);
    };
    tick();

    const onResize = () => {
      if (!el.clientWidth) return;
      camera.aspect = el.clientWidth / height;
      camera.updateProjectionMatrix();
      renderer.setSize(el.clientWidth, height);
    };
    window.addEventListener("resize", onResize);

    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("resize", onResize);
      // WebGL contexts are a finite browser resource — a dashboard left open
      // for hours across route changes will exhaust them without this.
      scene.traverse((obj) => {
        const mesh = obj as THREE.Mesh;
        mesh.geometry?.dispose?.();
        const mat = mesh.material as THREE.Material | THREE.Material[] | undefined;
        if (Array.isArray(mat)) mat.forEach((m) => m.dispose());
        else mat?.dispose?.();
      });
      renderer.dispose();
      if (renderer.domElement.parentNode === el) el.removeChild(renderer.domElement);
    };
    // Rebuild only when the set of providers changes, not on every poll.
  }, [providers.map((p) => p.id).join(","), height]);

  return <div ref={mount} style={{ width: "100%", height }} aria-hidden="true" />;
}
