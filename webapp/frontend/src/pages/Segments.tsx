import { useEffect, useState } from "react";
import { api, Camera, Segment } from "../api.ts";

export function SegmentsPage() {
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [cameraId, setCameraId] = useState<string>("");
  const [segments, setSegments] = useState<Segment[]>([]);
  const [selected, setSelected] = useState<Segment | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listCameras()
      .then((cams) => {
        setCameras(cams);
        if (cams.length > 0 && !cameraId) setCameraId(cams[0].id);
      })
      .catch((e) => setError(String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!cameraId) return;
    setSelected(null);
    api
      .listSegments(cameraId, 200)
      .then(setSegments)
      .catch((e) => setError(String(e)));
  }, [cameraId]);

  const toggleProtect = async (seg: Segment) => {
    try {
      await api.setSegmentProtected(seg.path, !seg.protected);
      setSegments((rows) =>
        rows.map((r) => (r.path === seg.path ? { ...r, protected: !r.protected } : r)),
      );
      if (selected?.path === seg.path) {
        setSelected({ ...seg, protected: !seg.protected });
      }
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div>
      {error && <div className="error">{error}</div>}

      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 16 }}>
        <label style={{ color: "#8892a7", fontSize: 13 }}>Camera</label>
        <select value={cameraId} onChange={(e) => setCameraId(e.target.value)}>
          {cameras.length === 0 && <option value="">(no cameras configured)</option>}
          {cameras.map((c) => (
            <option key={c.id} value={c.id}>
              {c.id} — {c.role}
            </option>
          ))}
        </select>
        <span style={{ color: "#8892a7", fontSize: 12 }}>{segments.length} segments</span>
      </div>

      <div className="segment-list">
        <div className="left">
          {segments.length === 0 && (
            <div style={{ padding: 16, color: "#8892a7", fontSize: 13 }}>
              No segments yet. Start the capture service and refresh.
            </div>
          )}
          {segments.map((seg) => (
            <button
              key={seg.path}
              className={selected?.path === seg.path ? "selected" : ""}
              onClick={() => setSelected(seg)}
            >
              {seg.started_at.slice(0, 19).replace("T", " ")}
              {seg.protected && " 🔒"}
              <span className="meta">
                {seg.duration_s.toFixed(1)}s · {(seg.size_bytes / 1e6).toFixed(1)} MB ·{" "}
                {seg.codec}
              </span>
            </button>
          ))}
        </div>
        <div className="right">
          {selected ? (
            <div>
              <video
                key={selected.path}
                controls
                autoPlay
                src={api.segmentVideoUrl(selected.path)}
              />
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginTop: 12,
                  fontSize: 13,
                  color: "#8892a7",
                }}
              >
                <div>{selected.path}</div>
                <button
                  className="subtle"
                  onClick={() => toggleProtect(selected)}
                >
                  {selected.protected ? "Unprotect" : "Mark as incident"}
                </button>
              </div>
            </div>
          ) : (
            <div className="empty">Select a segment to play</div>
          )}
        </div>
      </div>
    </div>
  );
}
