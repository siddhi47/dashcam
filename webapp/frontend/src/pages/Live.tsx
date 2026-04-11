import { useEffect, useState } from "react";
import { api, Camera } from "../api.ts";

export function LivePage() {
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listCameras()
      .then(setCameras)
      .catch((e) => setError(String(e)));
  }, []);

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
        {cameras.map((cam) => (
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
              <div style={{ fontSize: 14, fontWeight: 600 }}>{cam.id}</div>
              <div style={{ fontSize: 11, color: "#8892a7" }}>
                {cam.role} · {cam.resolution} · {cam.fps} fps · {cam.codec}
              </div>
            </div>
            {/*
             * MJPEG preview stream. The browser handles multipart/x-mixed-replace
             * in an <img> tag natively — one long-lived HTTP connection, one
             * visible image that updates as new frames arrive. Disconnecting
             * (unmount / tab switch) closes the connection and the capture
             * service drops the subscriber.
             */}
            <img
              src={api.livePreviewUrl(cam.id)}
              alt={`${cam.id} live preview`}
              style={{
                width: "100%",
                display: "block",
                background: "#000",
                aspectRatio: "16 / 9",
                objectFit: "cover",
              }}
              onError={() => {
                // Browsers silently stop loading broken MJPEG streams; we
                // could retry here but for now just show the black
                // placeholder until the user refreshes.
              }}
            />
            <div
              style={{
                padding: "8px 16px",
                fontSize: 11,
                color: "#8892a7",
                fontFamily: "monospace",
              }}
            >
              mxid: {cam.mxid}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
