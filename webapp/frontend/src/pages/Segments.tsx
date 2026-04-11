import { useEffect, useMemo, useState } from "react";
import { api, Camera, Segment } from "../api.ts";

const PAGE_SIZE = 500;

// -----------------------------------------------------------------------------
// Grouping helpers
//
// All grouping happens in the browser, in the viewer's local timezone. The
// backend stores started_at as UTC — we parse it once with `new Date(...)` and
// format everything from there so "Today" means today *where the user is*, not
// today in UTC.
// -----------------------------------------------------------------------------

/** A clip with its parsed timestamp, kept alongside the original for display. */
interface EnrichedSegment {
  seg: Segment;
  date: Date;
  dateKey: string; // YYYY-MM-DD in local time
  hourKey: string; // YYYY-MM-DDTHH in local time
}

function enrich(seg: Segment): EnrichedSegment {
  const date = new Date(seg.started_at);
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  const h = String(date.getHours()).padStart(2, "0");
  return {
    seg,
    date,
    dateKey: `${y}-${m}-${d}`,
    hourKey: `${y}-${m}-${d}T${h}`,
  };
}

function dateKeyToday(now = new Date()): string {
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, "0");
  const d = String(now.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function dateKeyDaysAgo(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return dateKeyToday(d);
}

function formatDateHeader(dateKey: string): string {
  const today = dateKeyToday();
  const yesterday = dateKeyDaysAgo(1);
  if (dateKey === today) return "Today";
  if (dateKey === yesterday) return "Yesterday";
  // Any other date: format as "Apr 9 2026" (short month, day, year).
  const [y, m, d] = dateKey.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function formatHourHeader(hourKey: string): string {
  const hour = Number(hourKey.split("T")[1]);
  const d = new Date();
  d.setHours(hour, 0, 0, 0);
  return d.toLocaleTimeString(undefined, { hour: "numeric", hour12: true });
}

function formatDuration(totalSeconds: number): string {
  const total = Math.round(totalSeconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

interface HourGroup {
  hourKey: string;
  segments: EnrichedSegment[];
}

interface DateGroup {
  dateKey: string;
  segments: EnrichedSegment[];
  hours: HourGroup[];
}

/**
 * Turn a flat newest-first array of segments into Date → Hour → [segments]
 * buckets, preserving the newest-first ordering inside each bucket.
 */
function groupByDateAndHour(enriched: EnrichedSegment[]): DateGroup[] {
  const byDate = new Map<string, EnrichedSegment[]>();
  for (const e of enriched) {
    const bucket = byDate.get(e.dateKey);
    if (bucket) bucket.push(e);
    else byDate.set(e.dateKey, [e]);
  }

  const result: DateGroup[] = [];
  for (const [dateKey, segments] of byDate) {
    const byHour = new Map<string, EnrichedSegment[]>();
    for (const e of segments) {
      const bucket = byHour.get(e.hourKey);
      if (bucket) bucket.push(e);
      else byHour.set(e.hourKey, [e]);
    }
    const hours: HourGroup[] = Array.from(byHour, ([hourKey, segs]) => ({
      hourKey,
      segments: segs,
    }));
    // Hours sorted newest-first within a date.
    hours.sort((a, b) => (a.hourKey < b.hourKey ? 1 : -1));
    result.push({ dateKey, segments, hours });
  }
  // Dates sorted newest-first.
  result.sort((a, b) => (a.dateKey < b.dateKey ? 1 : -1));
  return result;
}

function totalDuration(items: EnrichedSegment[]): number {
  return items.reduce((sum, e) => sum + e.seg.duration_s, 0);
}

// -----------------------------------------------------------------------------
// Page
// -----------------------------------------------------------------------------

export function SegmentsPage() {
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [cameraId, setCameraId] = useState<string>("");
  const [segments, setSegments] = useState<Segment[]>([]);
  const [selected, setSelected] = useState<Segment | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [loadingMore, setLoadingMore] = useState<boolean>(false);
  const [hasMore, setHasMore] = useState<boolean>(true);

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
    setSegments([]);
    setHasMore(true);
    setLoading(true);
    api
      .listSegments({ camera: cameraId, limit: PAGE_SIZE })
      .then((rows) => {
        setSegments(rows);
        setHasMore(rows.length === PAGE_SIZE);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [cameraId]);

  const loadMore = async () => {
    if (segments.length === 0 || loadingMore || !hasMore) return;
    setLoadingMore(true);
    try {
      const oldest = segments[segments.length - 1];
      const more = await api.listSegments({
        camera: cameraId,
        limit: PAGE_SIZE,
        before: oldest.started_at,
      });
      setSegments((prev) => [...prev, ...more]);
      setHasMore(more.length === PAGE_SIZE);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoadingMore(false);
    }
  };

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

  // Re-derive groups whenever the flat list changes.
  const { protectedSegs, dateGroups, todayKey, currentHourKey } = useMemo(() => {
    const enriched = segments.map(enrich);
    const pinned = enriched.filter((e) => e.seg.protected);
    const rest = enriched.filter((e) => !e.seg.protected);
    const groups = groupByDateAndHour(rest);
    const now = new Date();
    return {
      protectedSegs: pinned,
      dateGroups: groups,
      todayKey: dateKeyToday(now),
      currentHourKey: `${dateKeyToday(now)}T${String(now.getHours()).padStart(2, "0")}`,
    };
  }, [segments]);

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
        <span style={{ color: "#8892a7", fontSize: 12 }}>
          {segments.length} loaded
          {hasMore && " (more available)"}
        </span>
      </div>

      <div className="segment-list">
        <div className="left">
          {loading && segments.length === 0 && (
            <div style={{ padding: 16, color: "#8892a7", fontSize: 13 }}>Loading…</div>
          )}
          {!loading && segments.length === 0 && (
            <div style={{ padding: 16, color: "#8892a7", fontSize: 13 }}>
              No segments yet. Start the capture service and refresh.
            </div>
          )}

          {/* Pinned protected section — always at the top if non-empty. */}
          {protectedSegs.length > 0 && (
            <details open className="group">
              <summary className="group-header">
                <span>⭐ Protected</span>
                <span className="group-meta">
                  {protectedSegs.length} · {formatDuration(totalDuration(protectedSegs))}
                </span>
              </summary>
              <div className="group-body">
                {protectedSegs.map((e) => (
                  <SegmentButton
                    key={e.seg.path}
                    segment={e.seg}
                    date={e.date}
                    selected={selected?.path === e.seg.path}
                    onSelect={() => setSelected(e.seg)}
                  />
                ))}
              </div>
            </details>
          )}

          {dateGroups.map((dg) => (
            <details
              key={dg.dateKey}
              open={dg.dateKey === todayKey}
              className="group"
            >
              <summary className="group-header">
                <span>{formatDateHeader(dg.dateKey)}</span>
                <span className="group-meta">
                  {dg.segments.length} · {formatDuration(totalDuration(dg.segments))}
                </span>
              </summary>
              <div className="group-body">
                {dg.hours.map((hg) => (
                  <details
                    key={hg.hourKey}
                    open={hg.hourKey === currentHourKey}
                    className="group group-inner"
                  >
                    <summary className="group-header">
                      <span>{formatHourHeader(hg.hourKey)}</span>
                      <span className="group-meta">
                        {hg.segments.length} · {formatDuration(totalDuration(hg.segments))}
                      </span>
                    </summary>
                    <div className="group-body">
                      {hg.segments.map((e) => (
                        <SegmentButton
                          key={e.seg.path}
                          segment={e.seg}
                          date={e.date}
                          selected={selected?.path === e.seg.path}
                          onSelect={() => setSelected(e.seg)}
                        />
                      ))}
                    </div>
                  </details>
                ))}
              </div>
            </details>
          ))}

          {hasMore && segments.length > 0 && (
            <button
              className="subtle"
              style={{ margin: 12, width: "calc(100% - 24px)" }}
              onClick={loadMore}
              disabled={loadingMore}
            >
              {loadingMore ? "Loading…" : "Load older"}
            </button>
          )}
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
                <button className="subtle" onClick={() => toggleProtect(selected)}>
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

// -----------------------------------------------------------------------------
// Sub-components
// -----------------------------------------------------------------------------

interface SegmentButtonProps {
  segment: Segment;
  date: Date;
  selected: boolean;
  onSelect: () => void;
}

function SegmentButton({ segment, date, selected, onSelect }: SegmentButtonProps) {
  const timeStr = date.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  return (
    <button
      className={selected ? "selected seg-row" : "seg-row"}
      onClick={onSelect}
    >
      <span>
        {timeStr}
        {segment.protected && " 🔒"}
      </span>
      <span className="meta">
        {segment.duration_s.toFixed(0)}s · {(segment.size_bytes / 1e6).toFixed(1)} MB
      </span>
    </button>
  );
}
