"use client";

import {
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import {
  ArrowDown,
  ArrowLeft,
  Captions,
  HardDrive,
  ArrowRight,
  ArrowUp,
  Check,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  Folder,
  ListTodo,
  LoaderCircle,
  PanelRightClose,
  PanelRightOpen,
  RefreshCw,
  Search,
  Trash2,
  X,
} from "lucide-react";
import { useT } from "next-i18next/client";
import { usePathname } from "next/navigation";
import {
  directoryFromPathname,
  directoryPathname,
  rememberMediaDirectory,
} from "./directoryRouting";
import { AppHeader } from "./AppHeader";
import {
  accumulateAiUsage,
  AI_USAGE_STORAGE_KEY,
  parseStoredAiUsage,
  type AiUsage,
} from "./aiUsage";
import { filterAndSortEntries, type EntrySort } from "./fileEntries";
import { getSubtitleLanguage } from "./subtitleLanguages";
import { formatSubtitleModeLabel } from "./subtitleOptions";

type SidecarSubtitle = {
  name: string;
  path: string;
  language?: string | null;
};

type FileEntry = {
  name: string;
  path: string;
  type: "directory" | "video";
  size?: number | null;
  modified?: string | null;
  subtitles?: SidecarSubtitle[];
};

type JobResult = {
  outputPath?: string;
  existing?: boolean;
  reusedSourceSidecar?: boolean;
  sourceLanguage?: string;
  release?: string;
  moviehashMatch?: boolean;
  quota?: { remaining?: number | null; resetTimeUtc?: string | null };
  aiUsage?: AiUsage;
  from?: string;
  to?: string;
};

type JobItem = {
  path: string;
  status: string;
  message: string;
  started_at?: number | null;
  finished_at?: number | null;
  error?: string | null;
  result?: JobResult | null;
};

type Job = {
  id: string;
  kind?: "subtitles" | "rename";
  status: string;
  message: string;
  items: JobItem[];
  created_at: number;
  finished_at?: number | null;
  error?: string | null;
  target_language?: string;
  subtitle_mode?: string;
};

type Health = {
  ready: boolean;
  configuration: string;
  binaries: { ffmpeg: boolean; ffprobe: boolean };
};

type Option = { value: string; label: string };
type SettingsResponse = {
  setup_required: boolean;
  values: Record<string, string>;
  options: { target_languages: Option[]; subtitle_modes: Option[] };
};
type DirectoryLoadOptions = { refresh?: boolean; resetView?: boolean };

const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";
const TERMINAL = new Set(["completed", "failed"]);
const FILE_LIST_SKELETON_ROWS = 10;
const AI_USAGE_CHANGED_EVENT = "cue-ai-usage-changed";

function subscribeToAiUsage(onStoreChange: () => void) {
  function handleStorage(event: StorageEvent) {
    if (event.key === AI_USAGE_STORAGE_KEY || event.key === null) onStoreChange();
  }

  window.addEventListener("storage", handleStorage);
  window.addEventListener(AI_USAGE_CHANGED_EVENT, onStoreChange);
  return () => {
    window.removeEventListener("storage", handleStorage);
    window.removeEventListener(AI_USAGE_CHANGED_EVENT, onStoreChange);
  };
}

function getAiUsageSnapshot() {
  try {
    return localStorage.getItem(AI_USAGE_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

function getServerAiUsageSnapshot() {
  return "";
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${response.status})`);
  }
  return response.json();
}

function formatSize(bytes?: number | null) {
  if (!bytes) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

function formatElapsedTime(startedAt?: number | null, finishedAt?: number | null) {
  if (startedAt == null) return "0:00";
  const elapsed = Math.max(0, Math.floor((finishedAt ?? Date.now() / 1000) - startedAt));
  const hours = Math.floor(elapsed / 3600);
  const minutes = Math.floor((elapsed % 3600) / 60);
  const seconds = elapsed % 60;

  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${minutes}:${String(seconds).padStart(2, "0")}`;
}

export function Home() {
  const { t, i18n } = useT("common");
  const pathname = usePathname();
  const directoryRequest = useRef(0);
  const renameDialog = useRef<HTMLDialogElement>(null);
  const renameHelpDialog = useRef<HTMLDialogElement>(null);
  const jobsDock = useRef<HTMLDivElement>(null);
  const modeMenu = useRef<HTMLDetailsElement>(null);
  const submittingJob = useRef(false);
  const [starting, setStarting] = useState(false);
  const [health, setHealth] = useState<Health | null>(null);
  const [path, setPath] = useState("");
  const [location, setLocation] = useState("");
  const [openingFolder, setOpeningFolder] = useState(false);
  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<EntrySort>({ key: "modified", direction: "desc" });
  const [languageColumnCollapsed, setLanguageColumnCollapsed] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [queueMinimized, setQueueMinimized] = useState(true);
  const [initialQuotaRemaining, setInitialQuotaRemaining] = useState<number | null>(null);
  const [actionMode, setActionMode] = useState<"subtitles" | "rename">("subtitles");
  const [subtitleMode, setSubtitleMode] = useState("bilingual");
  const [subtitleModes, setSubtitleModes] = useState<Option[]>([]);
  const [targetLanguageName, setTargetLanguageName] = useState("");
  const [sourceType, setSourceType] = useState<"webdav" | "local">("webdav");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [renameTitle, setRenameTitle] = useState("");
  const [renameError, setRenameError] = useState("");
  const [error, setError] = useState("");
  const storedAiUsage = useSyncExternalStore(
    subscribeToAiUsage,
    getAiUsageSnapshot,
    getServerAiUsageSnapshot,
  );
  const aiUsage = parseStoredAiUsage(storedAiUsage);
  const loadDirectory = useCallback(async (
    nextPath: string,
    { refresh = false, resetView = true }: DirectoryLoadOptions = {},
  ) => {
    const request = ++directoryRequest.current;
    setRefreshing(refresh);
    setLoading(!refresh);
    setError("");
    setLocation("");
    if (resetView) {
      setPath(nextPath);
      setEntries([]);
      setSelected([]);
      setQuery("");
    }
    try {
      const data = await api<{ path: string; entries: FileEntry[]; location: string }>(
        `/api/files?path=${encodeURIComponent(nextPath)}${refresh ? "&refresh=true" : ""}`,
      );
      if (request !== directoryRequest.current) return;
      setPath(data.path);
      setEntries(data.entries);
      setLocation(data.location);
      rememberMediaDirectory(sessionStorage, data.path);
    } catch (reason) {
      if (request !== directoryRequest.current) return;
      setError(reason instanceof Error ? reason.message : t("home.errors.loadDirectory"));
    } finally {
      if (request === directoryRequest.current) {
        setRefreshing(false);
        setLoading(false);
      }
    }
  }, [t]);

  function navigateDirectory(nextPath: string) {
    const href = directoryPathname(nextPath, i18n.language);
    if (window.location.pathname !== href) window.history.pushState(null, "", href);
  }

  async function openLocalFolder() {
    setOpeningFolder(true);
    setError("");
    try {
      await api(`/api/folders/open?path=${encodeURIComponent(path)}`, { method: "POST" });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("home.openFolderError"));
    } finally {
      setOpeningFolder(false);
    }
  }

  useEffect(() => {
    if (!health?.ready) return;
    let active = true;
    const requests = directoryRequest;
    void Promise.resolve().then(() => {
      if (!active) return;
      try {
        void loadDirectory(directoryFromPathname(pathname));
      } catch (reason) {
        setEntries([]);
        setSelected([]);
        setPath("");
        setLoading(false);
        setRefreshing(false);
        setError(reason instanceof Error ? reason.message : t("home.errors.loadDirectory"));
      }
    });
    return () => {
      active = false;
      requests.current++;
    };
  }, [pathname, health?.ready, loadDirectory, t]);

  useEffect(() => {
    api<Health>("/api/health")
      .then((value) => {
        setHealth(value);
        if (value.ready) {
          void api<{ remaining: number }>("/api/quota")
            .then(({ remaining }) => setInitialQuotaRemaining(remaining))
            .catch(() => setInitialQuotaRemaining(null));
        } else setLoading(false);
      })
      .catch((reason) => {
        setError(reason instanceof Error ? reason.message : t("home.errors.backendUnavailable"));
        setLoading(false);
      });

    api<SettingsResponse>("/api/settings")
      .then(({ values, options, setup_required }) => {
        if (setup_required) {
          window.location.replace(`${i18n.language === "en" ? "" : `/${i18n.language}`}/settings/`);
          return;
        }
        setSubtitleModes(options.subtitle_modes);
        setTargetLanguageName(
          options.target_languages.find(({ value }) => value === values.target_language)?.label
            ?? values.target_language,
        );
        setSourceType(values.source_type === "local" ? "local" : "webdav");
        if (options.subtitle_modes.some(({ value }) => value === values.default_subtitle_mode)) {
          setSubtitleMode(values.default_subtitle_mode);
        }
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : t("home.errors.loadSubtitleSettings")));

    const remembered = (sessionStorage.getItem("cue-jobs")
      ?? sessionStorage.getItem("cue-job")
      ?? "").split(",").filter(Boolean);
    if (remembered.length) Promise.all(remembered.map((id) => api<Job>(`/api/jobs/${id}`).catch(() => null)))
      .then((values) => setJobs(values.filter((value): value is Job => Boolean(value))));
  }, [t, i18n.language]);

  useEffect(() => {
    const active = jobs.filter((job) => !TERMINAL.has(job.status));
    if (!active.length) return;
    const timer = window.setTimeout(() => {
      Promise.all(active.map((job) => api<Job>(`/api/jobs/${job.id}`)))
        .then((updated) => {
          setJobs((current) => current.map((job) => updated.find(({ id }) => id === job.id) ?? job));
          if (updated.some((job) => TERMINAL.has(job.status))) {
            void loadDirectory(path, { refresh: true, resetView: false });
          }
        })
        .catch((reason) => setError(String(reason)));
    }, 1000);
    return () => window.clearTimeout(timer);
  }, [jobs, loadDirectory, path]);

  useEffect(() => {
    try {
      const stored = parseStoredAiUsage(localStorage.getItem(AI_USAGE_STORAGE_KEY));
      const accumulated = accumulateAiUsage(stored, jobs);
      if (accumulated.countedItems.length === stored.countedItems.length) return;
      localStorage.setItem(AI_USAGE_STORAGE_KEY, JSON.stringify(accumulated));
      window.dispatchEvent(new Event(AI_USAGE_CHANGED_EVENT));
    } catch {
      // Browser storage may be unavailable in private or restricted contexts.
    }
  }, [jobs]);

  useEffect(() => {
    if (queueMinimized) return;

    function dismissQueue(event: PointerEvent) {
      if (renameHelpDialog.current?.open) return;
      if (!jobsDock.current?.contains(event.target as Node)) setQueueMinimized(true);
    }

    function dismissOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape" && !renameHelpDialog.current?.open) {
        setQueueMinimized(true);
        jobsDock.current?.querySelector("button")?.focus();
      }
    }
    document.addEventListener("pointerdown", dismissQueue);
    document.addEventListener("keydown", dismissOnEscape);
    return () => {
      document.removeEventListener("pointerdown", dismissQueue);
      document.removeEventListener("keydown", dismissOnEscape);
    };
  }, [queueMinimized]);

  useEffect(() => {
    function dismissMenu(event: PointerEvent) {
      if (!modeMenu.current?.contains(event.target as Node)) modeMenu.current?.removeAttribute("open");
    }
    function dismissOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape" && modeMenu.current?.open) {
        modeMenu.current.removeAttribute("open");
        modeMenu.current.querySelector("summary")?.focus();
      }
    }
    document.addEventListener("pointerdown", dismissMenu);
    document.addEventListener("keydown", dismissOnEscape);
    return () => {
      document.removeEventListener("pointerdown", dismissMenu);
      document.removeEventListener("keydown", dismissOnEscape);
    };
  }, []);

  const crumbs = useMemo(() => {
    const parts = path ? path.split("/") : [];
    return [
      { name: t("header.media"), path: "" },
      ...parts.map((name, index) => ({ name, path: parts.slice(0, index + 1).join("/") })),
    ];
  }, [path, t]);

  const visibleEntries = useMemo(
    () => filterAndSortEntries(entries, query, sort),
    [entries, query, sort],
  );
  const selectedPaths = entries
    .filter((entry) => entry.type === "video" && selected.includes(entry.path))
    .map((entry) => entry.path);

  function changeSort(key: EntrySort["key"]) {
    setSort((current) => current.key === key
      ? { key, direction: current.direction === "asc" ? "desc" : "asc" }
      : { key, direction: key === "name" ? "asc" : "desc" });
  }

  const queueJob = useCallback((job: Job) => {
    setJobs((current) => {
      const next = [...current, job];
      sessionStorage.setItem("cue-jobs", next.map(({ id }) => id).join(","));
      sessionStorage.removeItem("cue-job");
      return next;
    });
  }, []);

  function clearFinishedJobs() {
    setJobs((current) => {
      const next = current.filter((job) => !TERMINAL.has(job.status));
      sessionStorage.setItem("cue-jobs", next.map(({ id }) => id).join(","));
      return next;
    });
  }

  const handleStart = useCallback(async () => {
    if (!selectedPaths.length || submittingJob.current) return;
    submittingJob.current = true;
    setStarting(true);
    setError("");
    try {
      const value = await api<{ jobId: string }>("/api/jobs", {
        method: "POST",
        body: JSON.stringify({ paths: selectedPaths, mode: subtitleMode }),
      });
      const queued = {
        id: value.jobId,
        status: "queued",
        message: t("home.jobs.waiting"),
        created_at: Date.now() / 1000,
        subtitle_mode: subtitleMode,
        items: selectedPaths.map((path) => ({ path, status: "queued", message: t("home.jobs.waiting") })),
      };
      queueJob(queued);
      setSelected([]);
      setQueueMinimized(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("home.errors.startJob"));
    } finally {
      submittingJob.current = false;
      setStarting(false);
    }
  }, [selectedPaths, subtitleMode, t, queueJob]);

  const handleRename = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedPaths.length) return;
    setRenaming(true);
    setRenameError("");
    try {
      const value = await api<{ jobId: string }>("/api/rename", {
        method: "POST",
        body: JSON.stringify({ paths: selectedPaths, title: renameTitle }),
      });
      renameDialog.current?.close();
      queueJob({
        id: value.jobId,
        kind: "rename",
        status: "queued",
        message: t("home.jobs.waiting"),
        created_at: Date.now() / 1000,
        items: selectedPaths.map((path) => ({ path, status: "queued", message: t("home.jobs.waiting") })),
      });
      setSelected([]);
    } catch (reason) {
      setRenameError(reason instanceof Error ? reason.message : t("home.errors.renameVideos"));
    } finally {
      setRenaming(false);
    }
  }, [selectedPaths, renameTitle, t, queueJob]);

  const items = jobs.flatMap((job) => job.items);
  const activeItems = jobs.filter((job) => !TERMINAL.has(job.status)).flatMap((job) => job.items);
  const pendingItems = activeItems.filter((item) => !TERMINAL.has(item.status));
  const busy = pendingItems.length > 0;
  const selectablePaths = visibleEntries
    .filter((entry) => entry.type === "video" && !pendingItems.some((item) => item.path === entry.path))
    .map((entry) => entry.path);
  const allVisibleSelected = selectablePaths.length > 0 && selectablePaths.every((path) => selected.includes(path));
  const [, setElapsedTimerTick] = useState(0);
  const completed = items.filter((item) => item.status === "completed").length;
  const failed = items.filter((item) => item.status === "failed").length;
  const totalTokens = aiUsage.totalTokens;
  const quotaRemaining = items.reduce<number | null>(
    (remaining, item) => item.result?.quota?.remaining ?? remaining,
    initialQuotaRemaining,
  );
  const selectedSubtitleMode = subtitleModes.find(({ value }) => value === subtitleMode);
  const actionModeLabel = actionMode === "rename"
    ? t("home.actions.aiNaming")
    : selectedSubtitleMode
      ? formatSubtitleModeLabel(selectedSubtitleMode.label, targetLanguageName)
      : t("home.actions.loadingOptions");

  useEffect(() => {
    if (!busy) return;
    const timer = window.setInterval(() => setElapsedTimerTick((tick) => tick + 1), 1000);
    return () => window.clearInterval(timer);
  }, [busy]);

  return (
    <main>
      <AppHeader jobsControl={(
        <div className="jobsDock" ref={jobsDock}>
          <button
            className={`jobsButton${busy ? " busy" : ""}`}
            type="button"
            onClick={() => setQueueMinimized((current) => !current)}
            aria-label={busy
              ? t("home.jobs.buttonActive", { total: jobs.length, active: pendingItems.length })
              : t("home.jobs.button", { total: jobs.length })}
            aria-expanded={!queueMinimized}
            aria-controls="job-queue"
          >
            {busy
              ? <LoaderCircle className="spinner" size={16} strokeWidth={1.75} aria-hidden="true" />
              : <ListTodo size={16} strokeWidth={1.75} aria-hidden="true" />}
            <span>{t("home.jobs.title")}</span>
            <span className="jobsBadge" aria-hidden="true">{pendingItems.length}</span>
          </button>

          {!queueMinimized && (
            <aside id="job-queue" className="queue" aria-live="polite" aria-label={t("home.jobs.queueLabel")}>
              <div className="queueHeader">
                <div className="queueSummary">
                  {busy && <LoaderCircle className="spinner" size={16} strokeWidth={1.75} aria-hidden="true" />}
                  <span><strong>{completed}</strong> {t("home.jobs.done")} · <strong>{failed}</strong> {t("home.jobs.failed")}</span>
                  <b>{busy
                    ? t("home.jobs.active", { count: pendingItems.length })
                    : items.length ? t("home.jobs.allFinished") : t("home.jobs.none")}</b>
                </div>
                <div className="queueControls">
                  <button
                    className="queueIconButton"
                    type="button"
                    onClick={clearFinishedJobs}
                    disabled={!jobs.some((job) => TERMINAL.has(job.status))}
                    aria-label={t("home.jobs.clearFinished")}
                    aria-describedby="clear-jobs-tooltip"
                  >
                    <Trash2 size={14} strokeWidth={1.75} aria-hidden="true" />
                    <span className="queueTooltip" id="clear-jobs-tooltip" role="tooltip">
                      {t("home.jobs.clearHint")}
                    </span>
                  </button>
                </div>
              </div>
              <div className="queueItems">
                {items.length === 0 && <div className="queueEmpty"><ListTodo size={28} aria-hidden="true" /><strong>{t("home.jobs.empty")}</strong><span>{t("home.jobs.emptyHint")}</span></div>}
                {jobs.flatMap((job) => job.items.map((item, index) => {
                  const elapsedTime = formatElapsedTime(item.started_at, item.finished_at);
                  return (
                    <div className="queueItem" key={`${job.id}-${item.path}-${index}`}>
                      <div>
                        <strong title={item.path}>{item.path.split("/").at(-1)}</strong>
                        <small className={item.error ? "queueError" : ""} title={item.error ?? item.message}>{item.error ?? item.message}</small>
                        {item.error && (item.error === "No reliable requested subtitle was found" || /^OpenSubtitles search failed \(4\d\d\):.*\bquery\b.*\b(reject(?:ed)?|invalid)\b/i.test(item.error)) && (
                          <button
                            type="button"
                            className="renameHelpLink"
                            aria-haspopup="dialog"
                            aria-controls="rename-help"
                            onClick={() => renameHelpDialog.current?.showModal()}
                          >
                            {t("home.renameHelp.link")}
                          </button>
                        )}
                      </div>
                      <span className="queueTimer" role="timer" aria-label={t("home.jobs.elapsed", { time: elapsedTime })}>
                        {elapsedTime}
                      </span>
                      <span className={`stage ${item.status}`}>{item.status.replaceAll("_", " ")}</span>
                    </div>
                  );
                }))}
              </div>
            </aside>
          )}
        </div>
      )} refreshControl={(
        <button
          className="refreshButton"
          type="button"
          onClick={() => void loadDirectory(path, { refresh: true, resetView: false })}
          disabled={loading || refreshing || !health?.ready}
          aria-label={refreshing ? t("home.refreshing") : t("home.refresh")}
          title={refreshing ? t("home.refreshing") : t("home.refresh")}
        >
          <RefreshCw className={refreshing ? "spinner" : undefined} size={16} strokeWidth={1.75} aria-hidden="true" />
        </button>
      )} />

      <section className="libraryIntro">
        <div><h2>{t("home.library")}</h2></div>
        <span className="sourceBadge"><HardDrive size={15} aria-hidden="true" />{sourceType === "local" ? t("home.local") : "WebDAV"}</span>
      </section>

      {error && <p className="error" role="alert">{error}</p>}
      {health && !health.ready && (
        <section className="notice" role="alert">
          <strong>{t("home.setup.incomplete")}</strong>
          <span>{health.configuration}</span>
          {!health.binaries.ffmpeg && <span>{t("home.setup.ffmpegMissing")}</span>}
          {!health.binaries.ffprobe && <span>{t("home.setup.ffprobeMissing")}</span>}
        </section>
      )}

      <section className="workspace" aria-label={t("home.browserLabel", { source: sourceType === "local" ? t("home.local") : "WebDAV" })}>
        <div className="directoryHeader">
          <nav className="breadcrumbs" aria-label={t("home.directoryPath")}>
            {path && <button className="parentFolder" type="button" disabled={loading} aria-label={t("home.parentFolder")} onClick={() => navigateDirectory(crumbs.at(-2)?.path ?? "")}><ArrowLeft size={16} aria-hidden="true" /></button>}
            {crumbs.map((crumb, index) => (
              <span className={index === crumbs.length - 1 ? "current" : undefined} key={crumb.path || "root"}>
                {index > 0 && <ChevronRight size={14} strokeWidth={1.75} aria-hidden="true" />}
                <button
                  type="button"
                  title={crumb.name}
                  onClick={() => navigateDirectory(crumb.path)}
                  disabled={loading}
                  aria-current={index === crumbs.length - 1 ? "page" : undefined}
                >
                  {index === 0 && <HardDrive size={14} aria-hidden="true" />}{crumb.name}
                </button>
              </span>
            ))}
          </nav>
          {sourceType === "local" && (
            <button className="openFolderButton" type="button" onClick={() => void openLocalFolder()} disabled={!location || loading || openingFolder} title={`${t("home.openLocalFolder")}${location ? `: ${location}` : ""}`} aria-label={t("home.openLocalFolder")} aria-busy={openingFolder}>
              {openingFolder ? <LoaderCircle className="spinner" size={16} aria-hidden="true" /> : <ExternalLink size={16} strokeWidth={1.75} aria-hidden="true" />}
              <span>{t("home.openFolder")}</span>
            </button>
          )}
        </div>

        <div className="tableTools">
          <label className="searchField">
            <Search size={16} strokeWidth={1.75} aria-hidden="true" />
            <input
              type="search"
              aria-label={t("home.searchFolder")}
              placeholder={t("home.searchFolder")}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <div className="selectionTools">
            <span className="itemCount">{loading ? t("home.loadingDirectory") : t("home.itemCount", { count: visibleEntries.length })}</span>
            {selectablePaths.length > 0 && <button type="button" className="textButton" onClick={() => setSelected((current) => allVisibleSelected
              ? current.filter((path) => !selectablePaths.includes(path))
              : [...new Set([...current, ...selectablePaths])])}>{allVisibleSelected ? t("home.deselectVisible") : t("home.selectVisible")}</button>}
          </div>
        </div>

        <div className={`table${languageColumnCollapsed ? " languageCollapsed" : ""}`} aria-label={t("home.videos")}>
          <div className="tableHead">
            <button
              type="button"
              aria-label={t("home.sortBy", {
                field: t("home.columns.name"),
                current: sort.key === "name" ? t(`home.sort.${sort.direction}`) : "",
              })}
              onClick={() => changeSort("name")}
            >
              {t("home.columns.name")}
              {sort.key === "name" && (sort.direction === "asc"
                ? <ArrowUp size={12} strokeWidth={1.75} aria-hidden="true" />
                : <ArrowDown size={12} strokeWidth={1.75} aria-hidden="true" />)}
            </button>
            <span>{t("home.columns.size")}</span>
            <button
              type="button"
              aria-label={t("home.sortBy", {
                field: t("home.columns.date"),
                current: sort.key === "modified" ? t(`home.sort.${sort.direction}`) : "",
              })}
              onClick={() => changeSort("modified")}
            >
              {t("home.columns.date")}
              {sort.key === "modified" && (sort.direction === "asc"
                ? <ArrowUp size={12} strokeWidth={1.75} aria-hidden="true" />
                : <ArrowDown size={12} strokeWidth={1.75} aria-hidden="true" />)}
            </button>
            <button
              className="languageToggle"
              type="button"
              aria-expanded={!languageColumnCollapsed}
              aria-label={languageColumnCollapsed ? t("home.showLanguage") : t("home.hideLanguage")}
              title={languageColumnCollapsed ? t("home.showLanguage") : t("home.hideLanguage")}
              onClick={() => setLanguageColumnCollapsed((collapsed) => !collapsed)}
            >
              <span className="languageToggleLabel">{t("home.columns.language")}</span>
              <span className="languageToggleIcons" aria-hidden="true">
                <PanelRightClose className="languageToggleCloseIcon" size={15} strokeWidth={1.75} />
                <PanelRightOpen className="languageToggleOpenIcon" size={15} strokeWidth={1.75} />
              </span>
            </button>
          </div>
          {loading && (
            <div className="loadingRows" role="status" aria-label={t("home.loadingDirectory")}>
              {Array.from({ length: FILE_LIST_SKELETON_ROWS }, (_, index) => (
                <div className="row loadingRow" key={index} aria-hidden="true">
                  <span className="name">
                    <span className="skeleton skeletonIcon" />
                    <span className="skeleton skeletonName" />
                  </span>
                  <span className="skeleton skeletonSize" />
                  <span className="skeleton skeletonDate" />
                  <span className="skeleton skeletonLanguage languageCell" />
                </div>
              ))}
            </div>
          )}
          {!loading && entries.length === 0 && <div className="empty"><Folder size={32} aria-hidden="true" /><strong>{t("home.emptyFolder")}</strong><span>{t("home.emptyHint")}</span></div>}
          {!loading && entries.length > 0 && visibleEntries.length === 0 && <div className="empty"><Search size={32} aria-hidden="true" /><strong>{t("home.noMatches")}</strong><button type="button" className="textButton" onClick={() => setQuery("")}>{t("home.clearSearch")}</button></div>}
          {!loading && visibleEntries.map((entry) => entry.type === "directory" ? (
            <button type="button" className="row folder" key={entry.path} onClick={() => navigateDirectory(entry.path)}>
              <span className="name"><i aria-hidden="true"><Folder size={16} strokeWidth={1.75} /></i><span className="fileName" title={entry.name}>{entry.name}</span></span><span>{t("home.folder")}</span><span>{entry.modified ? new Date(entry.modified).toLocaleDateString(i18n.language) : "—"}</span><span className="languageCell folderArrow" aria-hidden="true"><ChevronRight size={16} /></span>
            </button>
          ) : (
            <label className={`row ${selected.includes(entry.path) ? "selected" : ""}`} key={entry.path}>
              <span className="name">
                <span className="checkboxWrap">
                  <input
                    type="checkbox"
                    value={entry.path}
                    checked={selected.includes(entry.path)}
                    onChange={(event) => setSelected((current) => event.target.checked
                      ? [...current, entry.path]
                      : current.filter((path) => path !== entry.path))}
                    disabled={pendingItems.some((item) => item.path === entry.path)}
                  />
                  <span className="checkboxControl" aria-hidden="true"><Check size={12} strokeWidth={2.25} /></span>
                </span>
                <span className="fileName" title={entry.name}>{entry.name}</span>
              </span>
              <span>{formatSize(entry.size)}</span>
              <span>{entry.modified ? new Date(entry.modified).toLocaleDateString(i18n.language) : "—"}</span>
              <span className="languageCell">
                {entry.subtitles?.length ? entry.subtitles.map((subtitle) => {
                  const language = getSubtitleLanguage(subtitle.language);
                  return (
                    <span
                      className="languageFlag"
                      role="img"
                      aria-label={t("home.subtitleLabel", { language: language.label })}
                      title={`${language.label} · ${subtitle.name}`}
                      key={subtitle.path}
                    >
                      {language.flag}
                    </span>
                  );
                }) : <span aria-label={t("home.noSidecarSubtitle")}>—</span>}
              </span>
            </label>
          ))}
        </div>

        <div className="actions">
          <div className="selectionSummary" aria-live="polite">
            <span className={`selectionIcon${selectedPaths.length ? " hasSelection" : ""}`}><Captions size={22} aria-hidden="true" /></span>
            <div className="selectionDetails">
              <strong>{selectedPaths.length ? t("home.selectedCount", { count: selectedPaths.length }) : t("home.selectPrompt")}</strong>
              <div className="usage" aria-label={t("home.usage.title")}>
                <span className="usagePill"><strong>{totalTokens.toLocaleString(i18n.language)}</strong> {t("home.usage.aiTokens")}</span>
                <span className="usagePill"><strong>{quotaRemaining ?? "—"}</strong> {t("home.usage.subtitlesRemaining")}</span>
              </div>
            </div>
            {selectedPaths.length > 0 && <button type="button" className="textButton" onClick={() => setSelected([])}>{t("home.clearSelection")}</button>}
          </div>
          <div className="createSplit">
            <button
              className="start"
              type="button"
              onClick={() => {
                if (actionMode === "rename") {
                  setRenameTitle("");
                  setRenameError("");
                  renameDialog.current?.showModal();
                } else void handleStart();
              }}
              disabled={!selectedPaths.length || !health?.ready || renaming || starting || loading || (actionMode === "subtitles" && !subtitleModes.length)}
            >
              <span>{starting ? t("home.actions.starting") : actionMode === "rename"
                ? t("home.actions.renameFilesCount", { count: selectedPaths.length })
                : busy
                  ? t("home.actions.addToQueue", { count: selectedPaths.length })
                  : t("home.actions.createSubtitles", { count: selectedPaths.length })}</span>
              <small>{actionModeLabel}</small>
            </button>
            {subtitleModes.length > 0 && <details className="modeMenu" ref={modeMenu}>
              <summary aria-label={t("home.actions.choose")} title={t("home.actions.choose")}>
                <ChevronDown size={16} strokeWidth={1.75} aria-hidden="true" />
              </summary>
              <div className="modeOptions"><p className="modeLabel">{t("home.actions.outputFormat")}</p>
                {subtitleModes.map((option) => (
                  <button
                    type="button"
                    key={option.value}
                    aria-current={actionMode === "subtitles" && option.value === subtitleMode ? "true" : undefined}
                    onClick={(event) => {
                      setActionMode("subtitles");
                      setSubtitleMode(option.value);
                      event.currentTarget.closest("details")?.removeAttribute("open");
                    }}
                  >
                    <span aria-hidden="true">{actionMode === "subtitles" && option.value === subtitleMode && <Check size={14} strokeWidth={1.75} />}</span>
                    {formatSubtitleModeLabel(option.label, targetLanguageName)}
                  </button>
                ))}
                <button
                  type="button"
                  aria-current={actionMode === "rename" ? "true" : undefined}
                  onClick={(event) => {
                    setActionMode("rename");
                    event.currentTarget.closest("details")?.removeAttribute("open");
                  }}
                >
                  <span aria-hidden="true">{actionMode === "rename" && <Check size={14} strokeWidth={1.75} />}</span>{t("home.actions.smartRename")}
                </button>
              </div>
            </details>}
          </div>
        </div>
      </section>

      <dialog
        id="rename-help"
        className="renameDialog"
        ref={renameHelpDialog}
        aria-labelledby="rename-help-title"
        aria-describedby="rename-help-reason"
        onClick={(event) => {
          if (event.target === event.currentTarget) event.currentTarget.close();
        }}
      >
        <div className="renameHelpContent">
          <div className="dialogHeader">
            <h2 id="rename-help-title">{t("home.renameHelp.title")}</h2>
            <button type="button" aria-label={t("home.renameHelp.close")} onClick={() => renameHelpDialog.current?.close()}>
              <X size={18} strokeWidth={1.75} aria-hidden="true" />
            </button>
          </div>
          <p id="rename-help-reason">{t("home.renameHelp.reason")}</p>
          <ol>
            <li>{t("home.renameHelp.select")}</li>
            <li>{t("home.renameHelp.choose")}</li>
            <li>{t("home.renameHelp.titleStep")}</li>
            <li>{t("home.renameHelp.retry")}</li>
          </ol>
          <p>{t("home.renameHelp.note")}</p>
          <div className="dialogActions">
            <button type="button" onClick={() => renameHelpDialog.current?.close()}>{t("home.renameHelp.done")}</button>
          </div>
        </div>
      </dialog>

      <dialog
        className="renameDialog"
        ref={renameDialog}
        aria-labelledby="rename-title"
        onCancel={(event) => {
          if (renaming) event.preventDefault();
        }}
        onClick={(event) => {
          if (event.target === event.currentTarget && !renaming) event.currentTarget.close();
        }}
      >
        <form onSubmit={handleRename}>
          <div className="dialogHeader">
            <div>
              <p className="eyebrow">{t("home.rename.eyebrow")}</p>
              <h2 id="rename-title">{t("home.rename.title", { count: selectedPaths.length })}</h2>
            </div>
            <button type="button" aria-label={t("home.rename.close")} onClick={() => renameDialog.current?.close()} disabled={renaming}>
              <X size={18} strokeWidth={1.75} aria-hidden="true" />
            </button>
          </div>
          <label htmlFor="english-title">{t("home.rename.englishTitle")}</label>
          <input
            id="english-title"
            value={renameTitle}
            onChange={(event) => setRenameTitle(event.target.value)}
            placeholder={t("home.rename.placeholder")}
            maxLength={200}
            autoFocus
            required
            disabled={renaming}
          />
          <p>{t("home.rename.help")}</p>
          {renameError && <p className="dialogError" role="alert">{renameError}</p>}
          <div className="dialogActions">
            <button type="button" onClick={() => renameDialog.current?.close()} disabled={renaming}>{t("home.rename.cancel")}</button>
            <button className="start" type="submit" disabled={renaming || !renameTitle.trim()}>
              {renaming ? t("home.rename.renaming") : t("home.rename.renameFiles")}<ArrowRight size={16} strokeWidth={1.75} aria-hidden="true" />
            </button>
          </div>
        </form>
      </dialog>

      <footer>{t("home.footer", { source: sourceType === "local" ? t("home.localLower") : "WebDAV" })}</footer>
    </main>
  );
}
