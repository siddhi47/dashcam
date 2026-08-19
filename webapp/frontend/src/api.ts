// Thin typed client for the oak-dashcam HTTP API.
// Paths are relative so Vite's dev-server proxy (vite.config.ts) can forward
// them to the FastAPI backend, and the built bundle works when served from
// the same origin as the API.

export type CameraRole = "front" | "rear" | "cabin" | "left" | "right";
export type Resolution = "720p" | "1080p" | "4k";
export type Codec = "h264" | "h265";

export type RotationDegrees = 0 | 180;

export interface Camera {
  id: string;
  mxid: string;
  role: CameraRole;
  resolution: Resolution;
  fps: number;
  codec: Codec;
  bitrate_kbps: number;
  rotation_degrees: RotationDegrees;
}

export interface CameraCreate {
  id: string;
  mxid: string;
  role: CameraRole;
  resolution: Resolution;
  fps: number;
  codec: Codec;
  bitrate_kbps: number;
  rotation_degrees: RotationDegrees;
}

export interface Segment {
  id: string;
  camera_id: string;
  path: string;
  started_at: string;
  duration_s: number;
  size_bytes: number;
  codec: string;
  protected: boolean;
}

export interface Detection {
  label: number;
  label_name: string;
  confidence: number;
  /** [xmin, ymin, xmax, ymax], normalized 0..1 over the full 16:9 frame. */
  bbox: [number, number, number, number];
}

export interface DetectionsResponse {
  /** false when no YOLO model is loaded on the capture side. */
  enabled: boolean;
  ts: number | null;
  detections: Detection[];
}

export interface DiscoveredCamera {
  mxid: string;
  assigned: boolean;
}

export interface DiscoveryResponse {
  cameras: DiscoveredCamera[];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  if (!resp.ok) {
    let detail: string;
    try {
      const body = await resp.json();
      detail = body.detail || JSON.stringify(body);
    } catch {
      detail = await resp.text();
    }
    throw new Error(`${resp.status} ${resp.statusText}: ${detail}`);
  }
  if (resp.status === 204) return undefined as T;
  return resp.json() as Promise<T>;
}

export const api = {
  listCameras: () => request<Camera[]>("/api/cameras"),

  createCamera: (cam: CameraCreate) =>
    request<{ camera: Camera; restart_required: true }>("/api/cameras", {
      method: "POST",
      body: JSON.stringify(cam),
    }),

  updateCamera: (id: string, cam: Omit<CameraCreate, "id">) =>
    request<{ camera: Camera; restart_required: true }>(`/api/cameras/${id}`, {
      method: "PUT",
      body: JSON.stringify(cam),
    }),

  deleteCamera: (id: string) =>
    request<{ deleted: string; restart_required: true }>(`/api/cameras/${id}`, {
      method: "DELETE",
    }),

  listSegments: (opts: { camera?: string; limit?: number; before?: string } = {}) => {
    const params = new URLSearchParams();
    if (opts.camera) params.set("camera", opts.camera);
    params.set("limit", String(opts.limit ?? 500));
    if (opts.before) params.set("before", opts.before);
    return request<Segment[]>(`/api/segments?${params.toString()}`);
  },

  setSegmentProtected: (segmentPath: string, protectedFlag: boolean) =>
    request<{ path: string; protected: boolean }>(
      `/api/segments/${segmentPath}/protect?protected=${protectedFlag}`,
      { method: "POST" },
    ),

  segmentVideoUrl: (segmentPath: string) => `/api/segments/${segmentPath}/video`,

  listDiscoveredCameras: () => request<DiscoveryResponse>("/api/discovery/cameras"),

  discoveryPreviewUrl: () => "/api/discovery/preview.mjpeg",

  livePreviewUrl: (cameraId: string) =>
    `/api/live/${encodeURIComponent(cameraId)}/preview.mjpeg`,

  getDetections: (cameraId: string) =>
    request<DetectionsResponse>(
      `/api/live/${encodeURIComponent(cameraId)}/detections`,
    ),

  resetCamera: (cameraId: string) =>
    request<{ status: string; camera_id: string }>(
      `/api/cameras/${encodeURIComponent(cameraId)}/reset`,
      { method: "POST" },
    ),

  rotateCamera: (cameraId: string) =>
    request<{ camera: Camera; restart_required: true }>(
      `/api/cameras/${encodeURIComponent(cameraId)}/rotate`,
      { method: "POST" },
    ),

  shutdownHost: () =>
    request<{ status: string }>("/api/system/shutdown", { method: "POST" }),
};
