// Pure route-reconstruction logic for the map dashboard.
//
// One source of truth, two consumers: this file is (1) inlined verbatim into
// viz_timeline_template.html at build time (replacing the MAPLOGIC marker),
// giving the browser a global `MAPLOGIC`, and (2) required directly by
// tests/freight_planner/maplogic.test.cjs so the geometry math is unit-tested
// without a browser. The module.exports line is guarded so it is inert in the
// browser (where `module` is undefined).
//
// Data model (from the timeline payload):
//   geom       : { "lat,lon|lat,lon": [[lat,lon],...] }  road polyline per directional leg
//   day.jobs[i]: { lat, lon, ty, clat, clon, ... }  a missing geocode is (0,0);
//                a `direct` carry carries its collect ORIGIN in clat/clon (dest in lat/lon)
//   snap stop  : [jobIdx, arriveMin, departMin, committed, tripIdx, reopt]
//                jobIdx>=0 -> day.jobs[jobIdx]; -2 -> depot->first; -1 -> last->depot
//   depotsByName: { NAME: {name, lat, lon} }  (vehicle.home names one)
const MAPLOGIC = (function () {
  function hasCoord(lat, lon) {
    // a real UK coordinate can have lon exactly 0 (Greenwich); the "no geocode"
    // sentinel in the jobs array is (0,0), so reject only that.
    return lat != null && lon != null && !(lat === 0 && lon === 0);
  }
  function pairKey(a, b) {
    return a[0].toFixed(5) + ',' + a[1].toFixed(5) + '|' + b[0].toFixed(5) + ',' + b[1].toFixed(5);
  }
  function legGeom(geom, a, b) {   // baked road polyline, or a straight segment on a miss
    return geom[pairKey(a, b)] || [a, b];
  }
  function depotCoord(veh, depotsByName) {
    const d = depotsByName[veh.home];
    return d && hasCoord(d.lat, d.lon) ? [d.lat, d.lon] : null;
  }
  // The ordered coords a stop contributes to the route. A DIRECT carry is a
  // customer->customer move, so it contributes its collect ORIGIN then its
  // deliver DEST (both drawn); every other stop is a single point.
  function stopCoords(day, veh, stop, depotsByName) {
    const ji = stop[0];
    if (ji === -2 || ji === -1) { const d = depotCoord(veh, depotsByName); return d ? [d] : []; }
    const j = day.jobs[ji];
    if (!j) return [];
    const out = [];
    if (j.ty === 'direct' && hasCoord(j.clat, j.clon)) out.push([j.clat, j.clon]);   // collect origin
    if (hasCoord(j.lat, j.lon)) out.push([j.lat, j.lon]);                            // deliver / service
    return out;
  }
  function routePolyline(geom, day, veh, snap, depotsByName) {
    const coords = [];
    for (const st of snap) for (const c of stopCoords(day, veh, st, depotsByName)) coords.push(c);
    const pts = [];
    for (let i = 0; i < coords.length - 1; i++) {
      const seg = legGeom(geom, coords[i], coords[i + 1]);
      for (const p of seg) {
        const last = pts[pts.length - 1];
        if (!last || last[0] !== p[0] || last[1] !== p[1]) pts.push(p);
      }
    }
    return pts;
  }

  // Per-stop "committed for display", matching the BOARD's 90-min frontier exactly
  // (so the map and board agree): a stop commits once the clock reaches `delta`
  // minutes before its DRIVE START (the previous stop's departure) — this is the
  // board's `firm || done` state. Connectors (-2/-1) are false. `snapEp` = the epoch
  // time of this snapshot, for the parked-at-floor "deferred" exception.
  function commitFlags(snap, snapEp, delta, t) {
    const flags = [];
    let prevD = null, prevTrip = null, tripStart = null;
    for (const s of snap) {
      const ji = s[0], arr = s[1], dep = s[2], cf = s[3], tr = s[4];
      if (tr !== prevTrip) { prevD = null; prevTrip = tr; tripStart = null; }
      if (ji === -2) { prevD = arr; tripStart = arr; flags.push(false); continue; }
      if (ji === -1) { prevD = dep; flags.push(false); continue; }
      const a = (arr != null ? arr : dep), d = (dep != null ? dep : arr);
      const ds = (prevD != null ? prevD : a);
      if (tripStart == null) tripStart = ds;
      const deferred = (tripStart != null && Math.abs(tripStart - (snapEp + delta)) < 3);
      const active = (!!cf || !deferred);
      flags.push(active && t >= ds - delta);   // firm OR done — both mean "committed to the driver"
      prevD = d;
    }
    return flags;
  }
  // the committed sub-snap = connectors + real stops whose commitFlags say committed
  function committedSnap(snap, flags) {
    return snap.filter((s, i) => s[0] < 0 || flags[i]);
  }

  function _hav(a, b) {   // rough metres, for splitting a direct carry's drive by leg length
    const R = 6371000, toR = Math.PI / 180;
    const dLat = (b[0] - a[0]) * toR, dLon = (b[1] - a[1]) * toR;
    const s = Math.sin(dLat / 2) ** 2 + Math.cos(a[0] * toR) * Math.cos(b[0] * toR) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(s)));
  }
  function _len(poly) { let L = 0; for (let i = 1; i < poly.length; i++) L += _hav(poly[i - 1], poly[i]); return L; }

  // Ordered {c, arr, dep} nodes for the committed route. A direct carry expands to
  // an origin node (a pass-through: the collect happens during the inbound drive, so
  // it gets no dwell and a time split from that drive by geometric length) plus a
  // dest node (the service window). This keeps the simulated truck ON the road it
  // actually drives, through the collect point.
  function committedTimedNodes(geom, day, veh, cSnap, depotsByName) {
    const seq = [];
    let hasReal = false, prevDep = null, prevC = null;
    for (const s of cSnap) {
      const ji = s[0];
      if (ji === -2 || ji === -1) {
        const d = depotCoord(veh, depotsByName); if (!d) continue;
        const tt = (ji === -2 ? s[1] : s[2]);
        seq.push({ c: d, arr: tt, dep: tt }); prevDep = tt; prevC = d; continue;
      }
      const cs = stopCoords(day, veh, s, depotsByName); if (!cs.length) continue;
      const arr = s[1], dep = s[2];
      if (cs.length === 2 && prevC && arr != null && prevDep != null) {   // direct: origin + dest
        const orig = cs[0], dest = cs[1];
        const l1 = _len(legGeom(geom, prevC, orig)), l2 = _len(legGeom(geom, orig, dest));
        const f = (l1 + l2 > 0) ? l1 / (l1 + l2) : 0.5;
        const ot = prevDep + f * (arr - prevDep);
        seq.push({ c: orig, arr: ot, dep: ot });
        seq.push({ c: dest, arr: arr, dep: dep });
        prevDep = dep; prevC = dest;
      } else {
        const c = cs[cs.length - 1];
        seq.push({ c: c, arr: arr, dep: dep }); prevDep = dep; prevC = c;
      }
      hasReal = true;
    }
    return hasReal ? seq : [];
  }
  // Ordered {c, ty, arr, dep, committed} nodes over the FULL snap (committed prefix +
  // uncommitted tail), carrying each stop's job TYPE and its commitment. A direct carry
  // expands to an origin pass-through node + a dest node, both typed 'direct'; depot
  // connectors are typed 'depot'. Powers routeSegments so the map colours each drive by
  // the job it serves (same palette as the board's job blocks).
  function routeTimedNodes(geom, day, veh, snap, flags, depotsByName) {
    const seq = [];
    let prevDep = null, prevC = null;
    for (let i = 0; i < snap.length; i++) {
      const s = snap[i], ji = s[0], committed = !!(flags && flags[i]);
      if (ji === -2 || ji === -1) {
        const d = depotCoord(veh, depotsByName); if (!d) continue;
        const tt = (ji === -2 ? s[1] : s[2]);
        seq.push({ c: d, ty: 'depot', arr: tt, dep: tt, committed });
        prevDep = tt; prevC = d; continue;
      }
      const j = day.jobs[ji]; if (!j) continue;
      const ty = j.ty || 'delivery';
      const cs = stopCoords(day, veh, s, depotsByName); if (!cs.length) continue;
      const arr = s[1], dep = s[2];
      if (cs.length === 2 && prevC && arr != null && prevDep != null) {   // direct: origin + dest
        const orig = cs[0], dest = cs[1];
        const l1 = _len(legGeom(geom, prevC, orig)), l2 = _len(legGeom(geom, orig, dest));
        const f = (l1 + l2 > 0) ? l1 / (l1 + l2) : 0.5;
        const ot = prevDep + f * (arr - prevDep);
        seq.push({ c: orig, ty, arr: ot, dep: ot, committed });
        seq.push({ c: dest, ty, arr, dep, committed });
        prevDep = dep; prevC = dest;
      } else {
        const c = cs[cs.length - 1];
        seq.push({ c, ty, arr, dep, committed });
        prevDep = dep; prevC = c;
      }
    }
    return seq;
  }

  // Per-leg drive segments from an ordered {c, ty, arr, committed} node list: each is the
  // road polyline into a node, coloured by that node's TYPE, tagged with its arrival (for
  // clock completion) and commitment. Segment i drives node[i] -> node[i+1] and "belongs to"
  // the destination it serves. Shared by normal routes (via routeSegments) and per-day tours.
  function segmentsFromNodes(geom, nodes) {
    const segs = [];
    for (let i = 0; i < nodes.length - 1; i++) {
      const a = nodes[i], b = nodes[i + 1];
      segs.push({ poly: legGeom(geom, a.c, b.c), ty: b.ty, arr: b.arr, committed: !!b.committed });
    }
    return segs;
  }
  function routeSegments(geom, day, veh, snap, flags, depotsByName) {
    return segmentsFromNodes(geom, routeTimedNodes(geom, day, veh, snap, flags, depotsByName));
  }

  // Simulated position at clock t, interpolated along an ordered {c, arr, dep} node list:
  // parked at the first node before its departure, dwelling while inside a node's window,
  // on the road (by geometry) between nodes, parked at the last node after arrival.
  function posAlongNodes(geom, nodes, t) {
    if (!nodes.length) return null;
    if (t <= nodes[0].dep) return nodes[0].c;                 // before departure: at the start
    for (let i = 0; i < nodes.length - 1; i++) {
      const a = nodes[i], b = nodes[i + 1];
      if (t >= a.arr && t <= a.dep) return a.c;               // dwelling at node a
      if (t > a.dep && t < b.arr) {                           // driving a -> b
        const line = legGeom(geom, a.c, b.c), span = b.arr - a.dep;
        const f = span > 0 ? (t - a.dep) / span : 0;
        const idx = Math.min(line.length - 1, Math.max(0, Math.round(f * (line.length - 1))));
        return line[idx];
      }
    }
    const last = nodes[nodes.length - 1];
    return t >= last.arr ? last.c : null;                     // after arrival: at the end
  }
  // simulated planned position along the COMMITTED route (the normal per-epoch route)
  function truckPos(geom, day, veh, cSnap, t, depotsByName) {
    return posAlongNodes(geom, committedTimedNodes(geom, day, veh, cSnap, depotsByName), t);
  }

  // A multi-day tour is stored SPLIT BY DAY (viz_timeline_build._tour_day_routes): each day is
  // one leg of a journey the truck never breaks to go home. Real customer stops keep their
  // planned arr/dep; the timeless anchor legs — depot-out (day 1), overnight-resume (a later
  // day), depot-return (the last day) — have no recorded times, so their drive TIME is
  // synthesized from road geometry purely so the puck moves and the strip shows the whole leg.
  // Speed = the tours' own planning speed (config.MULTIDAY_AVG_SPEED_KMH, motorway trunking) so
  // the synthesized legs are consistent with the customer stops' real times. No KPI depends on it.
  const TOUR_ANCHOR_KMH = 80;
  const TOUR_RETURN_DEPART = 6 * 60;   // nominal morning departure for a stop-less return leg
  function driveMin(geom, a, b) { return _len(legGeom(geom, a, b)) / 1000 / TOUR_ANCHOR_KMH * 60; }
  function tourDayNodes(geom, td) {
    if (!td) return [];
    const depot = (td.depot && hasCoord(td.depot[0], td.depot[1])) ? td.depot : null;
    const body = [];                                  // the day's real stops (directs expanded)
    for (const s of (td.stops || [])) {
      if (!hasCoord(s.lat, s.lon)) continue;
      const dest = [s.lat, s.lon];
      const info = { o: s.o, pc: s.pc, pco: s.pco, p: s.p, kg: s.kg };   // for the strip tooltip
      if (s.ty === 'direct' && hasCoord(s.clat, s.clon)) {
        body.push({ c: [s.clat, s.clon], ty: 'direct', arr: s.arr, dep: s.arr, _origin: true });
        body.push({ c: dest, ty: 'direct', arr: s.arr, dep: s.dep, ...info });
      } else {
        body.push({ c: dest, ty: s.ty || 'delivery', arr: s.arr, dep: s.dep, ...info });
      }
    }
    let startC = null;                                // depot on day 1, else the overnight point
    if (td.startDepot && depot) startC = depot;
    else if (td.resume && hasCoord(td.resume[0], td.resume[1])) startC = td.resume;

    const nodes = [];
    if (body.length) {
      const firstIsOrigin = !!body[0]._origin;        // a leading direct pass-through, not a dwell
      const firstArr = firstIsOrigin && body[1] ? body[1].arr : body[0].arr;
      if (startC) {
        const drive = firstIsOrigin && body[1]
          ? driveMin(geom, startC, body[0].c) + driveMin(geom, body[0].c, body[1].c)
          : driveMin(geom, startC, body[0].c);
        const dep = (td.startT != null) ? td.startT : ((firstArr != null) ? firstArr - drive : null);
        nodes.push({ c: startC, ty: 'depot', arr: dep, dep: dep, anchor: true });
      }
      for (const b of body) nodes.push(b);
      if (td.endDepot && depot) {
        const last = body[body.length - 1];
        const arr = (td.endT != null) ? td.endT
          : ((last.dep != null) ? last.dep + driveMin(geom, last.c, depot) : null);
        nodes.push({ c: depot, ty: 'depot', arr: arr, dep: arr, anchor: true });
      } else if (td.park && hasCoord(td.park[0], td.park[1])) {
        // mid-leg overnight: the day's driving continues past the last stop to the
        // sleep point (tour_overnight row), so the polyline + puck end there.
        const last = body[body.length - 1];
        const arr = (td.endT != null) ? td.endT
          : ((last.dep != null) ? last.dep + driveMin(geom, last.c, td.park) : null);
        nodes.push({ c: td.park, ty: 'overnight', arr: arr, dep: arr, anchor: true });
      }
    } else if (startC && td.endDepot && depot) {      // pure return day: just drive home
      const depart = (td.startT != null) ? td.startT : TOUR_RETURN_DEPART;
      const arr = (td.endT != null) ? td.endT : depart + driveMin(geom, startC, depot);
      nodes.push({ c: startC, ty: 'depot', arr: depart, dep: depart, anchor: true });
      nodes.push({ c: depot, ty: 'depot', arr: arr, dep: arr, anchor: true });
    }
    // re-time a direct-origin pass-through: split its inbound drive by geometry length (same
    // rule as committedTimedNodes), now that the preceding node's departure is known.
    for (let i = 1; i < nodes.length - 1; i++) {
      if (!nodes[i]._origin) continue;
      const prev = nodes[i - 1], dest = nodes[i + 1];
      const l1 = _len(legGeom(geom, prev.c, nodes[i].c)), l2 = _len(legGeom(geom, nodes[i].c, dest.c));
      const f = (l1 + l2 > 0) ? l1 / (l1 + l2) : 0.5;
      if (prev.dep != null && dest.arr != null) {
        const ot = prev.dep + f * (dest.arr - prev.dep);
        nodes[i].arr = ot; nodes[i].dep = ot;
      }
    }
    return nodes;
  }
  // Running load over the day for the map strip's utilization lines. Per trip (grouped by the
  // stop tuple's trip index): deliveries ride out of the depot pre-loaded and drop off; pickups
  // rise and ride to the depot; a direct carry is a transient bump (boards at arrive, drops at
  // depart). Returns step points { t, p, kg } = load AFTER time t. Capacity-agnostic.
  function loadProfile(snap, jobs) {
    const out = [];
    if (!snap || !snap.length) return out;
    const trips = new Map();
    for (const s of snap) { const tr = s[4]; if (!trips.has(tr)) trips.set(tr, []); trips.get(tr).push(s); }
    for (const stops of trips.values()) {
      const js = stops.filter(s => s[0] >= 0);
      let p = 0, kg = 0;
      for (const s of js) { const j = jobs[s[0]] || {}; if (j.ty === 'delivery') { p += (j.pallets || 0); kg += (j.kg || 0); } }
      const startT = (stops[0] && stops[0][2] != null) ? stops[0][2] : (js[0] ? js[0][1] : null);
      if (startT != null) out.push({ t: startT, p, kg });
      for (const s of js) {
        const j = jobs[s[0]] || {}, arr = s[1], dep = s[2];
        if (j.ty === 'delivery') { p -= (j.pallets || 0); kg -= (j.kg || 0); if (dep != null) out.push({ t: dep, p, kg }); }
        else if (j.ty === 'pickup') { p += (j.pallets || 0); kg += (j.kg || 0); if (dep != null) out.push({ t: dep, p, kg }); }
        else if (j.ty === 'direct') {
          // loaded at the ORIGIN and carried the whole leg (user rule 2026-07-16):
          // collection time estimated backward from the destination arrival at the
          // anchor speed over the origin->dest haversine (x1.3 road factor).
          const lastT = out.length ? out[out.length - 1].t : (arr != null ? arr : 0);
          let riseT = lastT;
          if (arr != null && j.clat != null && j.lat != null) {
            const km = _hav([j.clat, j.clon], [j.lat, j.lon]) / 1000 * 1.3;
            riseT = Math.max(lastT, arr - km / 80 * 60);
          }
          out.push({ t: riseT, p: p + (j.pallets || 0), kg: kg + (j.kg || 0) });
          if (arr != null) out.push({ t: arr, p, kg });
        }
      }
      const ret = stops.find(s => s[0] === -1);
      if (ret && ret[2] != null) out.push({ t: ret[2], p: 0, kg: 0 });
    }
    return out;
  }

  // Load-utilization step points for a TOUR day (tours live outside the snap
  // stream, so loadProfile has nothing to walk). Stops carry lp/lkg (load AFTER)
  // and p/kg (the leg's own quantity): a delivery's before = after + qty, a
  // pickup's = after - qty; a direct bumps transiently (arr up, dep down). The
  // truck carries the pre-first-stop load from the day start.
  function tourLoadProfile(td) {
    const stops = (td && td.stops) || [];
    if (!stops.length) return [];
    const first = stops[0];
    const before = (s) => {
      if (s.ty === "pickup") return { p: Math.max(0, (s.lp || 0) - (s.p || 0)),
                                      kg: Math.max(0, (s.lkg || 0) - (s.kg || 0)) };
      return { p: (s.lp || 0) + (s.ty === "delivery" ? (s.p || 0) : 0),
               kg: (s.lkg || 0) + (s.ty === "delivery" ? (s.kg || 0) : 0) };
    };
    const b0 = before(first);
    const out = [{ t: 0, p: b0.p, kg: b0.kg }];
    for (const s of stops) {
      const at = (s.arr == null) ? 0 : s.arr;
      if (s.ty === "direct") {
        // loaded at the ORIGIN, carried until unloaded at the destination
        let riseT = out[out.length - 1].t;
        if (s.clat != null && s.lat != null) {
          const km = _hav([s.clat, s.clon], [s.lat, s.lon]) / 1000 * 1.3;
          riseT = Math.max(riseT, at - km / 80 * 60);
        }
        out.push({ t: riseT, p: (s.lp || 0) + (s.p || 0), kg: (s.lkg || 0) + (s.kg || 0) });
        out.push({ t: at, p: s.lp || 0, kg: s.lkg || 0 });
      } else {
        out.push({ t: at, p: s.lp || 0, kg: s.lkg || 0 });
      }
    }
    const last = out[out.length - 1];
    out.push({ t: 1440, p: last.p, kg: last.kg });
    return out;
  }

  return { hasCoord, pairKey, legGeom, depotCoord, stopCoords, routePolyline,
           commitFlags, committedSnap, committedTimedNodes, routeTimedNodes,
           segmentsFromNodes, routeSegments, posAlongNodes, truckPos, tourDayNodes, loadProfile   , tourLoadProfile
  };
})();
if (typeof module !== 'undefined' && module.exports) module.exports = MAPLOGIC;
