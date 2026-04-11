import { useEffect, useState } from "react";
import { api, Camera } from "../api.ts";

// How long we disable the Reset/Rotate buttons after a click, and how
// long to wait before we force the <img> to reconnect to the preview URL.
// The supervisor backoff is 5s + a few seconds of device boot, so
// 12s is a comfortable "it should be back by now" window.
const RESET_WAIT_MS = 12_000;

export function LivePage() {
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [error, setError] = useState<string | null>(null);
  // Per-camera tick: when we reset or rotate, stash a timestamp so we
  // can (a) disable the buttons until the supervisor is back up and
  // (b) cache-bust the <img> src to force a reconnect. React keys the
  // <img> off this value so the tag is torn down + rebuilt cleanly.
  const [tick, setTick] = useState<Record<string, number>>({});
  const [pending, setPending] = useState<Record<string, boolean>>({});

  const refreshCameras = () =>
    api
      .listCameras()
      .then(setCameras)
      .catch((e) => setError(String(e)));

  useEffect(() => {
    refreshCameras();
  }, []);

  const scheduleReconnect = (cameraId: string) => {
    setTimeout(() => {
      setTick((t) => ({ ...t, [cameraId]: Date.now() }));
      setPending((p) => ({ ...p, [cameraId]: false }));
      // Refresh the camera list so the rotation badge updates to the
      // new value after a rotate.
      refreshCameras();
    }, RESET_WAIT_MS);
  };

  const reset = async (cam: Camera) => {
    if (pending[cam.id]) return;
    setPending((p) => ({ ...p, [cam.id]: true }));
    setError(null);
    try {
      await api.resetCamera(cam.id);
    } catch (e) {
      setError(`Reset failed: ${e}`);
      setPending((p) => ({ ...p, [cam.id]: false }));
      return;
    }
    scheduleReconnect(cam.id);
  };

  const rotate = async (cam: Camera) => {
    if (pending[cam.id]) return;
    setPending((p) => ({ ...p, [cam.id]: true }));
    setError(null);
    try {
      // Optimistic update so the badge flips immediately.
      const newRotation = cam.rotation_degrees === 180 ? 0 : 180;
      setCameras((rows) =>
        rows.map((r) =>
          r.id === cam.id ? { ...r, rotation_degrees: newRotation } : r,
        ),
      );
      await api.rotateCamera(cam.id);
    } catch (e) {
      setError(`Rotate failed: ${e}`);
      setPending((p) => ({ ...p, [cam.id]: false }));
      // Roll back the optimistic update by re-fetching.
      refreshCameras();
      return;
    }
    scheduleReconnect(cam.id);
  };

  return (
    <div>
      {error && <div className="error">{error}</div>}

      {cameras.length === 0 && !error && (
        <div className="banner">
          No cameras configured yet. Go to the <strong>Cameras</strong> tab to add one.
        </div>
      )}

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(480px, 1fr))",
          gap: 24,
        }}
      >
        {cameras.map((cam) => {
          const t = tick[cam.id] ?? 0;
          const isBusy = pending[cam.id] === true;
          const imgKey = `${cam.id}-${t}`;
          const imgSrc = t
            ? `${api.livePreviewUrl(cam.id)}?t=${t}`
            : api.livePreviewUrl(cam.id);
          return (
            <div
              key={cam.id}
              style={{
                border: "1px solid #1d212b",
                borderRadius: 8,
                background: "#0f131b",
                overflow: "hidden",
              }}
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "baseline",
                  padding: "12px 16px",
                  borderBottom: "1px solid #1d212b",
                }}
              >
                <div style={{ fontSize: 14, fontWeight: 600 }}>
                  {cam.id}
                  {cam.rotation_degrees === 180 && (
                    <span
                      style={{
                        marginLeft: 8,
                        fontSize: 10,
                        fontWeight: 500,
                        padding: "2px 6px",
                        borderRadius: 4,
                        background: "#2a2f3d",
                        color: "#8892a7",
                        verticalAlign: "middle",
                      }}
                    >
                      180°
                    </span>
                  )}
                </div>
                <div style={{ fontSize: 11, color: "#8892a7" }}>
                  {cam.role} · {cam.resolution} · {cam.fps} fps · {cam.codec}
                </div>
              </div>
              {/*
               * MJPEG preview stream. The browser handles multipart/x-mixed-replace
               * in an <img> tag natively — one long-lived HTTP connection, one
               * visible image that updates as new frames arrive. Disconnecting
               * (unmount / tab switch / reset / rotate) closes the connection
               * and the capture service drops the subscriber.
               */}
              <img
                key={imgKey}
                src={imgSrc}
                alt={`${cam.id} live preview`}
                style={{
                  width: "100%",
                  display: "block",
                  background: "#000",
                  aspectRatio: "16 / 9",
                  objectFit: "cover",
                }}
              />
              <div
                style={{
                  padding: "8px 16px 12px",
                  fontSize: 11,
                  color: "#8892a7",
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  gap: 12,
                }}
              >
                <div style={{ fontFamily: "monospace" }}>mxid: {cam.mxid}</div>
                <div style={{ display: "flex", gap: 8 }}>
                  <button
                    className="subtle"
                    onClick={() => rotate(cam)}
                    disabled={isBusy}
                    title="Flip this camera 180° (for ceiling-mounted installs). Toggles back to normal on a second click."
                  >
                    {isBusy ? "…" : "Rotate 180°"}
                  </button>
                  <button
                    className="subtle"
                    onClick={() => reset(cam)}
                    disabled={isBusy}
                    title="Stop and restart this camera's pipeline. Other cameras keep recording."
                  >
                    {isBusy ? "…" : "Reset"}
                  </button>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
