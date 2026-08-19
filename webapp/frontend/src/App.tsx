import { useEffect, useState } from "react";
import { LivePage } from "./pages/Live.tsx";
import { SegmentsPage } from "./pages/Segments.tsx";
import { CamerasPage } from "./pages/Cameras.tsx";
import { DisplayPage } from "./pages/Display.tsx";

type Tab = "live" | "segments" | "cameras";

export default function App() {
  // Default to display mode (fullscreen camera feeds). Navigate to
  // `#admin` to get the full management UI (Live, Segments, Cameras).
  // This makes the dashcam screen show feeds on boot with zero
  // interaction required.
  const [adminMode, setAdminMode] = useState(
    window.location.hash === "#admin",
  );

  useEffect(() => {
    const onHash = () => setAdminMode(window.location.hash === "#admin");
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  if (adminMode) {
    return <MainApp />;
  }

  return <DisplayPage />;
}

function MainApp() {
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
