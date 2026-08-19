import { useEffect, useRef, useState } from "react";
import { api, Camera } from "../api.ts";
import { DetectionOverlay } from "../components/DetectionOverlay.tsx";

const CONTROLS_TIMEOUT_MS = 4000;

export function DisplayPage() {
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [focused, setFocused] = useState<string | null>(null);
  const [showControls, setShowControls] = useState(false);
  const [shuttingDown, setShuttingDown] = useState(false);
  const hideTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const load = () =>
      api
        .listCameras()
        .then(setCameras)
        .catch(() => setTimeout(load, 5000));
    load();
  }, []);

  const flashControls = () => {
    setShowControls(true);
    if (hideTimer.current) clearTimeout(hideTimer.current);
    hideTimer.current = setTimeout(() => setShowControls(false), CONTROLS_TIMEOUT_MS);
  };

  const handleTapFeed = (camId: string) => {
    flashControls();
    if (focused === null) {
      setFocused(camId);
    } else if (focused === camId) {
      setFocused(null);
    } else {
      setFocused(camId);
    }
  };

  const handleShutdown = async () => {
    if (!confirm("Shut down the Pi? You will need to physically power-cycle to turn it back on.")) {
      return;
    }
    setShuttingDown(true);
    try {
      await api.shutdownHost();
    } catch {
      // If the request fails it might be because the Pi is already
      // going down. Either way, show the shutting-down state.
    }
  };

  if (shuttingDown) {
    return (
      <div
        style={{
          width: "100vw",
          height: "100vh",
          background: "#000",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: "#8892a7",
          fontSize: 16,
          fontFamily: "monospace",
        }}
      >
        shutting down...
      </div>
    );
  }

  if (cameras.length === 0) {
    return (
      <div
        style={{
          width: "100vw",
          height: "100vh",
          background: "#000",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: "#333",
          fontSize: 14,
          fontFamily: "monospace",
        }}
      >
        waiting for cameras...
      </div>
    );
  }

  const visibleCameras = focused
    ? cameras.filter((c) => c.id === focused)
    : cameras;

  const cols = visibleCameras.length <= 2 ? visibleCameras.length : 2;
  const rows = visibleCameras.length <= 2 ? 1 : 2;

  return (
    <div
      style={{
        width: "100vw",
        height: "100vh",
        margin: 0,
        padding: 0,
        background: "#000",
        overflow: "hidden",
        position: "relative",
        cursor: showControls ? "default" : "none",
      }}
      onClick={flashControls}
    >
      {/* Camera feeds */}
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "grid",
          gridTemplateColumns: `repeat(${cols}, 1fr)`,
          gridTemplateRows: `repeat(${rows}, 1fr)`,
          gap: 0,
        }}
      >
        {visibleCameras.map((cam) => (
          <div
            key={cam.id}
            style={{ position: "relative", overflow: "hidden" }}
          >
            <img
              src={api.livePreviewUrl(cam.id)}
              alt=""
              onClick={(e) => {
                e.stopPropagation();
                handleTapFeed(cam.id);
              }}
              style={{
                width: "100%",
                height: "100%",
                objectFit: "cover",
                display: "block",
                cursor: "pointer",
              }}
            />
            {/* YOLO bboxes. fit="cover" makes the SVG crop the same
              * way the img does, so boxes stay glued to objects even
              * when the tile aspect ratio crops the 16:9 stream. */}
            <DetectionOverlay cameraId={cam.id} fit="cover" />
          </div>
        ))}
      </div>

      {/* Overlay controls — visible on tap, fades after 4s */}
      <div
        style={{
          position: "absolute",
          bottom: 0,
          left: 0,
          right: 0,
          background: "linear-gradient(transparent, rgba(0,0,0,0.8))",
          padding: "32px 16px 16px",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          opacity: showControls ? 1 : 0,
          transition: "opacity 0.3s ease",
          pointerEvents: showControls ? "auto" : "none",
        }}
      >
        {/* Left: layout buttons */}
        <div style={{ display: "flex", gap: 8 }}>
          <OverlayButton
            active={focused === null}
            onClick={() => { setFocused(null); flashControls(); }}
            label="All"
          />
          {cameras.map((cam) => (
            <OverlayButton
              key={cam.id}
              active={focused === cam.id}
              onClick={() => { setFocused(cam.id); flashControls(); }}
              label={cam.id}
            />
          ))}
        </div>

        {/* Right: admin link + shutdown */}
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <a
            href="#admin"
            onClick={(e) => e.stopPropagation()}
            style={{
              color: "#8892a7",
              fontSize: 12,
              textDecoration: "none",
              padding: "10px 14px",
              borderRadius: 8,
              border: "1px solid rgba(255,255,255,0.15)",
              background: "rgba(255,255,255,0.05)",
            }}
          >
            Admin
          </a>
          <OverlayButton
            active={false}
            onClick={(e) => { e.stopPropagation(); handleShutdown(); }}
            label="Shutdown"
            danger
          />
        </div>
      </div>
    </div>
  );
}

function OverlayButton({
  active,
  onClick,
  label,
  danger = false,
}: {
  active: boolean;
  onClick: (e: React.MouseEvent) => void;
  label: string;
  danger?: boolean;
}) {
  let bg = "rgba(255,255,255,0.1)";
  let border = "1px solid rgba(255,255,255,0.2)";
  if (active) {
    bg = "rgba(90,169,255,0.3)";
    border = "1px solid #5aa9ff";
  }
  if (danger) {
    bg = "rgba(155,47,47,0.4)";
    border = "1px solid rgba(200,80,80,0.5)";
  }

  return (
    <button
      onClick={onClick}
      style={{
        background: bg,
        border,
        color: "#fff",
        padding: "10px 18px",
        borderRadius: 8,
        fontSize: 13,
        cursor: "pointer",
        minWidth: 44,
        minHeight: 44,
      }}
    >
      {label}
    </button>
  );
}
