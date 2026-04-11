import { useEffect, useState } from "react";
import { api, Camera, CameraCreate, DiscoveredCamera } from "../api.ts";

const EMPTY_FORM: CameraCreate = {
  id: "",
  mxid: "auto",
  role: "front",
  resolution: "1080p",
  fps: 30,
  codec: "h265",
  bitrate_kbps: 8000,
};

export function CamerasPage() {
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [restartRequired, setRestartRequired] = useState<boolean>(false);
  const [editing, setEditing] = useState<Camera | null>(null);
  const [form, setForm] = useState<CameraCreate>(EMPTY_FORM);
  const [showForm, setShowForm] = useState<boolean>(false);

  // Discovery state for the "Add camera" flow.
  const [discovering, setDiscovering] = useState<boolean>(false);
  const [discovered, setDiscovered] = useState<DiscoveredCamera[]>([]);
  const [selectedMxid, setSelectedMxid] = useState<string | null>(null);

  const refresh = async () => {
    try {
      setCameras(await api.listCameras());
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const startAdd = async () => {
    setEditing(null);
    setForm(EMPTY_FORM);
    setDiscovered([]);
    setSelectedMxid(null);
    setShowForm(true);
    setDiscovering(true);
    try {
      const resp = await api.listDiscoveredCameras();
      const unassigned = resp.cameras.filter((c) => !c.assigned);
      setDiscovered(unassigned);
      if (unassigned.length === 1) {
        // Auto-select the only camera and pin its MxID on the form.
        setSelectedMxid(unassigned[0].mxid);
        setForm((f) => ({ ...f, mxid: unassigned[0].mxid }));
      } else if (unassigned.length > 1) {
        setSelectedMxid(unassigned[0].mxid);
        setForm((f) => ({ ...f, mxid: unassigned[0].mxid }));
      }
    } catch (e) {
      setError(`Discovery failed: ${e}`);
    } finally {
      setDiscovering(false);
    }
  };

  const startEdit = (cam: Camera) => {
    setEditing(cam);
    setForm({ ...cam });
    setShowForm(true);
    setDiscovered([]);
    setSelectedMxid(null);
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      if (editing) {
        const { id: _id, ...rest } = form;
        void _id;
        await api.updateCamera(editing.id, rest);
      } else {
        await api.createCamera(form);
      }
      setShowForm(false);
      setRestartRequired(true);
      await refresh();
    } catch (err) {
      setError(String(err));
    }
  };

  const remove = async (cam: Camera) => {
    if (!confirm(`Delete camera "${cam.id}"?`)) return;
    try {
      await api.deleteCamera(cam.id);
      setRestartRequired(true);
      await refresh();
    } catch (err) {
      setError(String(err));
    }
  };

  return (
    <div>
      {restartRequired && (
        <div className="banner">
          Changes applied to the database. <strong>Restart the capture service</strong> to
          pick them up (it only reads camera config on startup).
        </div>
      )}
      {error && <div className="error">{error}</div>}

      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 16,
        }}
      >
        <div style={{ color: "#8892a7", fontSize: 13 }}>
          {cameras.length} camera{cameras.length === 1 ? "" : "s"} configured
        </div>
        <button className="primary" onClick={startAdd}>
          Add camera
        </button>
      </div>

      {cameras.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Role</th>
              <th>MxID</th>
              <th>Resolution</th>
              <th>FPS</th>
              <th>Codec</th>
              <th>Bitrate</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {cameras.map((c) => (
              <tr key={c.id}>
                <td>{c.id}</td>
                <td>{c.role}</td>
                <td style={{ fontFamily: "monospace", fontSize: 12 }}>{c.mxid}</td>
                <td>{c.resolution}</td>
                <td>{c.fps}</td>
                <td>{c.codec}</td>
                <td>{c.bitrate_kbps} kbps</td>
                <td style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                  <button className="subtle" onClick={() => startEdit(c)}>
                    Edit
                  </button>
                  <button className="danger" onClick={() => remove(c)}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {showForm && (
        <div style={{ marginTop: 32, paddingTop: 24, borderTop: "1px solid #1d212b" }}>
          <h2 style={{ fontSize: 14, marginTop: 0 }}>
            {editing ? `Edit ${editing.id}` : "Add camera"}
          </h2>

          {/* Discovery block — only shown when adding, not editing. */}
          {!editing && (
            <div style={{ marginBottom: 24 }}>
              {discovering && (
                <div style={{ color: "#8892a7", fontSize: 13, padding: 12 }}>
                  Scanning for connected cameras… this takes a few seconds per device.
                </div>
              )}

              {!discovering && discovered.length === 0 && (
                <div className="banner">
                  No unassigned cameras detected. Plug in a new OAK device and click{" "}
                  <button
                    className="subtle"
                    style={{ marginLeft: 4 }}
                    onClick={startAdd}
                  >
                    Re-scan
                  </button>
                </div>
              )}

              {!discovering && discovered.length > 0 && (
                <div>
                  <div
                    style={{
                      color: "#8892a7",
                      fontSize: 12,
                      marginBottom: 8,
                    }}
                  >
                    Found {discovered.length} unassigned camera
                    {discovered.length === 1 ? "" : "s"}. Live preview below —
                    shows whichever unassigned camera DepthAI opens first. If you
                    have multiple, unplug the ones you don't want first.
                  </div>

                  <div
                    style={{
                      display: "flex",
                      gap: 16,
                      alignItems: "flex-start",
                    }}
                  >
                    <img
                      src={api.discoveryPreviewUrl()}
                      alt="live preview"
                      style={{
                        width: 480,
                        height: 270,
                        background: "#000",
                        borderRadius: 6,
                        objectFit: "cover",
                        border: "1px solid #2a2f3d",
                      }}
                    />
                    <div>
                      <div
                        style={{
                          fontSize: 12,
                          color: "#8892a7",
                          marginBottom: 4,
                        }}
                      >
                        Detected MxIDs
                      </div>
                      {discovered.map((d) => (
                        <label
                          key={d.mxid}
                          style={{
                            display: "block",
                            padding: "6px 8px",
                            fontFamily: "monospace",
                            fontSize: 12,
                            cursor: "pointer",
                            color:
                              selectedMxid === d.mxid ? "#5aa9ff" : "#e6e8ec",
                          }}
                        >
                          <input
                            type="radio"
                            name="mxid"
                            value={d.mxid}
                            checked={selectedMxid === d.mxid}
                            onChange={() => {
                              setSelectedMxid(d.mxid);
                              setForm((f) => ({ ...f, mxid: d.mxid }));
                            }}
                            style={{ marginRight: 6 }}
                          />
                          {d.mxid}
                        </label>
                      ))}
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}

          <form onSubmit={submit}>
            <label>ID</label>
            <input
              value={form.id}
              onChange={(e) => setForm({ ...form, id: e.target.value })}
              disabled={!!editing}
              placeholder="front"
              required
            />

            <label>MxID</label>
            <input
              value={form.mxid}
              onChange={(e) => setForm({ ...form, mxid: e.target.value })}
              placeholder="auto or 18443010..."
              required
            />

            <label>Role</label>
            <select
              value={form.role}
              onChange={(e) => setForm({ ...form, role: e.target.value as Camera["role"] })}
            >
              <option value="front">front</option>
              <option value="rear">rear</option>
              <option value="cabin">cabin</option>
              <option value="left">left</option>
              <option value="right">right</option>
            </select>

            <label>Resolution</label>
            <select
              value={form.resolution}
              onChange={(e) =>
                setForm({ ...form, resolution: e.target.value as Camera["resolution"] })
              }
            >
              <option value="720p">720p</option>
              <option value="1080p">1080p</option>
              <option value="4k">4k</option>
            </select>

            <label>FPS</label>
            <input
              type="number"
              min={1}
              max={60}
              value={form.fps}
              onChange={(e) => setForm({ ...form, fps: Number(e.target.value) })}
            />

            <label>Codec</label>
            <select
              value={form.codec}
              onChange={(e) => setForm({ ...form, codec: e.target.value as Camera["codec"] })}
            >
              <option value="h265">h265</option>
              <option value="h264">h264</option>
            </select>

            <label>Bitrate (kbps)</label>
            <input
              type="number"
              min={500}
              max={50000}
              step={500}
              value={form.bitrate_kbps}
              onChange={(e) => setForm({ ...form, bitrate_kbps: Number(e.target.value) })}
            />

            <div className="full">
              <button type="button" className="subtle" onClick={() => setShowForm(false)}>
                Cancel
              </button>
              <button type="submit" className="primary">
                {editing ? "Save" : "Create"}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
