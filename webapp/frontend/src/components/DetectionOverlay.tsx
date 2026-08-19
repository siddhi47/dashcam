import { useEffect, useState } from "react";
import { api, Detection } from "../api.ts";

// Poll cadence for the detections snapshot. The NN runs at camera fps
// on the OAK, but 3-4 Hz overlay updates are plenty for "what is the
// camera seeing" purposes and keep the HTTP chatter negligible next to
// the MJPEG stream itself.
const POLL_MS = 300;

// Nominal coordinate space for the SVG. Matches the stream's 16:9
// aspect; the actual numbers are arbitrary since everything scales.
const VIEW_W = 1920;
const VIEW_H = 1080;

/**
 * Bounding-box overlay for a live MJPEG preview `<img>`.
 *
 * Render inside the same `position: relative` wrapper as the img. The
 * mapping trick: the SVG fills the wrapper and uses a 16:9 viewBox
 * with `preserveAspectRatio` mirroring the img's `object-fit` ("cover"
 * → "slice", "contain" → "meet"). The browser then applies the exact
 * same scale/crop to the boxes as it does to the video pixels, so
 * normalized bbox coordinates line up with no measurement code at all.
 *
 * Polling stops permanently (for this mount) once the backend reports
 * detection disabled — no model loaded means no reason to keep asking.
 * Transient errors (camera restarting, sidecar briefly down) keep the
 * poll alive.
 */
export function DetectionOverlay({
  cameraId,
  fit = "cover",
}: {
  cameraId: string;
  fit?: "cover" | "contain";
}) {
  const [detections, setDetections] = useState<Detection[]>([]);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const poll = async () => {
      if (cancelled) return;
      try {
        const resp = await api.getDetections(cameraId);
        if (cancelled) return;
        if (!resp.enabled) {
          setDetections([]);
          return; // detection is off on the capture side — stop polling
        }
        setDetections(resp.detections);
      } catch {
        // Transient (camera restarting, sidecar unreachable): clear
        // stale boxes but keep polling.
        if (!cancelled) setDetections([]);
      }
      timer = setTimeout(poll, POLL_MS);
    };

    poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [cameraId]);

  if (detections.length === 0) return null;

  return (
    <svg
      viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
      preserveAspectRatio={fit === "cover" ? "xMidYMid slice" : "xMidYMid meet"}
      style={{
        position: "absolute",
        inset: 0,
        width: "100%",
        height: "100%",
        pointerEvents: "none",
      }}
    >
      {detections.map((d, i) => {
        const [xmin, ymin, xmax, ymax] = d.bbox;
        const x = xmin * VIEW_W;
        const y = ymin * VIEW_H;
        const w = (xmax - xmin) * VIEW_W;
        const h = (ymax - ymin) * VIEW_H;
        // Stable per-class color: spread hues around the wheel so
        // e.g. "car" and "person" are visually distinct everywhere.
        const color = `hsl(${(d.label * 57) % 360} 85% 60%)`;
        const label = `${d.label_name} ${(d.confidence * 100).toFixed(0)}%`;
        // Anchor the label toward the side of the frame with more
        // room, judged by the box center. Under `slice` (cover) the
        // crop is symmetric around the center, so this also keeps
        // long labels on-screen when the tile crops the 16:9 frame.
        const nearRight = (xmin + xmax) / 2 > 0.5;
        return (
          <g key={i}>
            <rect
              x={x}
              y={y}
              width={w}
              height={h}
              fill="none"
              stroke={color}
              strokeWidth={4}
            />
            <text
              x={nearRight ? x + w - 6 : x + 6}
              textAnchor={nearRight ? "end" : "start"}
              // Keep the label inside the frame when the box touches
              // the top edge.
              y={y < 40 ? y + 34 : y - 10}
              fill={color}
              fontSize={32}
              fontFamily="monospace"
              paintOrder="stroke"
              stroke="rgba(0,0,0,0.7)"
              strokeWidth={6}
            >
              {label}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
