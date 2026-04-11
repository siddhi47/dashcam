import { useState } from "react";
import { LivePage } from "./pages/Live.tsx";
import { SegmentsPage } from "./pages/Segments.tsx";
import { CamerasPage } from "./pages/Cameras.tsx";

type Tab = "live" | "segments" | "cameras";

export default function App() {
  // Live preview is the landing page — most of the time you open this
  // app to glance at what the cameras are seeing right now, not to
  // dig through old recordings.
  const [tab, setTab] = useState<Tab>("live");

  return (
    <>
      <header>
        <h1>oak-dashcam</h1>
        <nav>
          <button
            className={tab === "live" ? "active" : ""}
            onClick={() => setTab("live")}
          >
            Live
          </button>
          <button
            className={tab === "segments" ? "active" : ""}
            onClick={() => setTab("segments")}
          >
            Segments
          </button>
          <button
            className={tab === "cameras" ? "active" : ""}
            onClick={() => setTab("cameras")}
          >
            Cameras
          </button>
        </nav>
      </header>
      <main>
        {tab === "live" && <LivePage />}
        {tab === "segments" && <SegmentsPage />}
        {tab === "cameras" && <CamerasPage />}
      </main>
    </>
  );
}
