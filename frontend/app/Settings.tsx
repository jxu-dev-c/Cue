"use client";

import { type FormEvent, useEffect, useRef, useState } from "react";
import { ArrowRight, BookOpen, Check, ExternalLink, FileJson, HardDrive, Captions, Cloud, Search, Sparkles, ShieldCheck } from "lucide-react";
import { useT } from "next-i18next/client";
import { useRouter } from "next/navigation";
import { AppHeader } from "./AppHeader";
import { NativeSelect } from "./NativeSelect";
import { LocalFolderPicker } from "./LocalFolderPicker";
import { formatSubtitleModeLabel } from "./subtitleOptions";

const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";
const OPENSUBTITLES_GUIDE_URL = "https://opensubtitles.tawk.help/article/getting-started";
type Option = { value: string; label: string };
type SettingsOptions = {
  target_languages: Option[];
  subtitle_modes: Option[];
  source_types: Option[];
  subtitle_destinations: Option[];
};
type SettingField = {
  name: string;
  labelKey: string;
  separatorBefore?: boolean;
  secret?: boolean;
  optional?: boolean;
  type?: string;
  options?: Option[];
  optionKey?: keyof SettingsOptions;
};
const SECTION_ICONS = { storage: HardDrive, subtitles: Captions, webdav: Cloud, openSubtitles: Search, openAI: Sparkles };

const SECTIONS: { titleKey: string; fields: SettingField[] }[] = [
  {
    titleKey: "storage",
    fields: [
      { name: "source_type", labelKey: "mediaSource", optionKey: "source_types" },
      { name: "subtitle_destination", labelKey: "saveSubtitles", optionKey: "subtitle_destinations" },
      { name: "local_scan_path", labelKey: "scanPath", separatorBefore: true },
      { name: "local_output_path", labelKey: "outputFolder", separatorBefore: true },
    ],
  },
  {
    titleKey: "subtitles",
    fields: [
      { name: "target_language", labelKey: "targetLanguage", optionKey: "target_languages" },
      { name: "default_subtitle_mode", labelKey: "defaultMode", optionKey: "subtitle_modes" },
      {
        name: "minimal_frequency_tier",
        labelKey: "minimalFrequencyTier",
        options: [1, 2, 3, 4, 5].map((tier) => ({ value: `${tier * 1000}`, label: `Beyond top ${tier}K` })),
      },
    ],
  },
  {
    titleKey: "webdav",
    fields: [
      { name: "webdav_username", labelKey: "username" },
      { name: "webdav_password", labelKey: "password", secret: true },
      { name: "webdav_endpoint", labelKey: "endpoint" },
      { name: "webdav_scan_path", labelKey: "scanPath" },
    ],
  },
  {
    titleKey: "openSubtitles",
    fields: [
      { name: "opensubtitles_api_key", labelKey: "apiKey", secret: true },
      { name: "opensubtitles_consumer_name", labelKey: "consumerName" },
      { name: "opensubtitles_username", labelKey: "username", optional: true },
      { name: "opensubtitles_password", labelKey: "password", secret: true, optional: true },
    ],
  },
  {
    titleKey: "openAI",
    fields: [
      { name: "openai_base_url", labelKey: "baseUrl", type: "url" },
      { name: "openai_api_key", labelKey: "apiKey", secret: true },
      { name: "openai_model_id", labelKey: "model" },
      {
        name: "openai_reasoning_effort",
        labelKey: "subtitleReasoningEffort",
        options: ["none", "minimal", "low", "medium", "high", "xhigh", "max"].map((value) => ({ value, label: value })),
      },
      {
        name: "openai_rename_reasoning_effort",
        labelKey: "renameReasoningEffort",
        options: ["none", "minimal", "low", "medium", "high", "xhigh", "max"].map((value) => ({ value, label: value })),
      },
    ],
  },
];

function fieldIsVisible(field: SettingField, values: Record<string, string>) {
  const source = values.source_type ?? "webdav";
  const destination = values.subtitle_destination ?? "source";
  if (field.name.startsWith("webdav_")) return source === "webdav";
  if (field.name === "local_scan_path") return source === "local";
  if (field.name === "local_output_path") return source === "webdav" && destination === "local";
  return true;
}

type SettingsResponse = {
  setup_required: boolean;
  credentials_path: string;
  credentials_file_exists: boolean;
  values: Record<string, string>;
  secrets: Record<string, boolean>;
  options: SettingsOptions;
};

export function Settings() {
  const { t, i18n } = useT("common");
  const router = useRouter();
  const [setup, setSetup] = useState(false);
  const [step, setStep] = useState(0);
  const stepHeading = useRef<HTMLHeadingElement>(null);
  const steps = ["storage", "subtitles", "openSubtitles", "openAI", "review"];
  const [values, setValues] = useState<Record<string, string>>({});
  const [secretValues, setSecretValues] = useState<Record<string, string>>({});
  const [storedSecrets, setStoredSecrets] = useState<Record<string, boolean>>({});
  const [options, setOptions] = useState<SettingsOptions>({
    target_languages: [],
    subtitle_modes: [],
    source_types: [],
    subtitle_destinations: [],
  });
  const [clearSecrets, setClearSecrets] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [credentialsPath, setCredentialsPath] = useState("");
  const [credentialsFileExists, setCredentialsFileExists] = useState(false);
  const [openingCredentials, setOpeningCredentials] = useState(false);
  const [savedValues, setSavedValues] = useState("{}");
  const dirty = JSON.stringify(values) !== savedValues || Object.values(secretValues).some(Boolean) || clearSecrets.length > 0;
  const targetLanguageName = options.target_languages
    .find(({ value }) => value === values.target_language)?.label ?? values.target_language;

  useEffect(() => {
    fetch(`${API}/api/settings`)
      .then(async (response) => {
        const body = await response.json();
        if (!response.ok) throw new Error(body.detail || t("settings.loadError"));
        const settings = body as SettingsResponse;
        setValues(settings.values);
        setSavedValues(JSON.stringify(settings.values));
        setStoredSecrets(settings.secrets);
        setOptions(settings.options);
        setSetup(settings.setup_required);
        setCredentialsPath(settings.credentials_path);
        setCredentialsFileExists(settings.credentials_file_exists);
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : t("settings.loadError")))
      .finally(() => setLoading(false));
  }, [t]);

  function goToStep(next: number) {
    setStep(next);
    setError("");
    requestAnimationFrame(() => {
      stepHeading.current?.focus();
      stepHeading.current?.scrollIntoView({ block: "start", behavior: "smooth" });
    });
  }

  async function openCredentialsFile() {
    if (openingCredentials) return;
    setOpeningCredentials(true);
    setError("");
    try {
      const response = await fetch(`${API}/api/settings/credentials/open`, { method: "POST" });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || t("settings.openCredentialsError"));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("settings.openCredentialsError"));
    } finally {
      setOpeningCredentials(false);
    }
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (loading || saving) return;
    if (setup && step < steps.length - 1) {
      goToStep(step + 1);
      return;
    }
    if (!setup && !dirty) return;
    setSaving(true);
    setMessage("");
    setError("");
    try {
      const response = await fetch(`${API}/api/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values, secrets: secretValues, clear_secrets: clearSecrets }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || t("settings.saveError"));
      setStoredSecrets((current) => {
        const next = { ...current };
        for (const [key, value] of Object.entries(secretValues)) if (value) next[key] = true;
        for (const key of clearSecrets) next[key] = false;
        return next;
      });
      setSecretValues({});
      setClearSecrets([]);
      setMessage(body.message);
      setSavedValues(JSON.stringify(values));
      if (setup) {
        setSetup(false);
        router.replace(`${i18n.language === "en" ? "" : `/${i18n.language}`}/`);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("settings.saveError"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <main className="settingsPage">
      <AppHeader page="settings" mediaDisabled={loading || setup} />

      <form className={`settingsCard${setup ? " setupCard" : ""}`} onSubmit={save}>
        <nav className="settingsNav" aria-label={t("settings.navigation")}>
          <p className="eyebrow">{t(setup ? "setup.eyebrow" : "settings.configuration")}</p>
          {setup ? <ol className="setupSteps">{steps.map((name, index) => <li key={name} aria-current={step === index ? "step" : undefined}>
            <span>{index < step ? <Check size={14} aria-hidden="true" /> : index + 1}</span>
            {t(name === "review" ? "setup.review" : `settings.sections.${name}`)}
          </li>)}</ol> : SECTIONS.filter((section) => section.fields.some((field) => fieldIsVisible(field, values))).map((section) => {
            const Icon = SECTION_ICONS[section.titleKey as keyof typeof SECTION_ICONS];
            return <a href={`#settings-${section.titleKey}`} key={section.titleKey}><Icon size={17} aria-hidden="true" />{t(`settings.sections.${section.titleKey}`)}</a>;
          })}
          <div className="settingsPrivacy">
            <ShieldCheck size={19} aria-hidden="true" />
            <span>
              {t("settings.credentialsNotice")}
              {!setup && credentialsFileExists && <button
                type="button"
                className="credentialsPathLink"
                title={t("settings.openCredentialsPath", { path: credentialsPath })}
                aria-label={t("settings.openCredentialsPath", { path: credentialsPath })}
                disabled={openingCredentials}
                onClick={() => void openCredentialsFile()}
              >
                <FileJson size={14} aria-hidden="true" />
                {t("settings.openCredentials")}
              </button>}
            </span>
          </div>
        </nav>
        <div className="settingsContent">
        <div className="settingsIntro">
          {setup && <p className="eyebrow" aria-live="polite">{t("setup.progress", { current: step + 1, total: steps.length })}</p>}
          <h2 ref={stepHeading} tabIndex={-1}>{t(setup ? (step === 4 ? "setup.reviewTitle" : "setup.title") : "settings.configuration")}</h2>
          {setup && <p>{t(step === 4 ? "setup.reviewHint" : "setup.intro")}</p>}
        </div>
        <div className="settingsGrid">
          {SECTIONS.map((section) => {
            if (setup && section.titleKey !== steps[step] && !(step === 0 && section.titleKey === "webdav")) return null;
            const fields = section.fields.filter((field) => fieldIsVisible(field, values));
            if (!fields.length) return null;
            const Icon = SECTION_ICONS[section.titleKey as keyof typeof SECTION_ICONS];
            return <fieldset key={section.titleKey} id={`settings-${section.titleKey}`} tabIndex={-1}>
              <legend><Icon size={18} aria-hidden="true" />{t(`settings.sections.${section.titleKey}`)}</legend>
              <p className={`sectionDescription${section.titleKey === "openSubtitles" ? " hasSectionLinks" : ""}`}>
                {t(`settings.descriptions.${section.titleKey}`)}
              </p>
              {section.titleKey === "openSubtitles" && <div className="sectionLinks" aria-label={t("settings.openSubtitlesLinks.label")}>
                <a href={OPENSUBTITLES_GUIDE_URL} target="_blank" rel="noopener noreferrer">
                  <BookOpen size={15} aria-hidden="true" />
                  {t("settings.openSubtitlesLinks.guide")}
                  <ExternalLink size={13} aria-hidden="true" />
                </a>
              </div>}
              <div className="sectionFields">
              {fields.map((field) => (
                <div className={`settingField${field.separatorBefore ? " settingFieldSeparated" : ""}`} key={field.name}>
                  <label htmlFor={field.name}>
                    {t(`settings.fields.${field.labelKey}`)}
                    {field.optional && !setup && <small>{t("settings.optional")}</small>}
                  </label>
                  {field.options || field.optionKey ? (
                    <NativeSelect
                      id={field.name}
                      value={values[field.name] ?? ""}
                      onChange={(event) => setValues((current) => ({
                        ...current,
                        [field.name]: event.target.value,
                        ...(field.name === "source_type" && event.target.value === "local"
                          ? { subtitle_destination: "source" }
                          : {}),
                      }))}
                      disabled={loading || saving}
                    >
                      {(field.options ?? options[field.optionKey!] ?? [])
                        .filter((option) => field.name !== "subtitle_destination"
                          || values.source_type !== "local"
                          || option.value === "source")
                        .map((option) => (
                        <option key={option.value} value={option.value}>
                          {field.name === "default_subtitle_mode"
                            ? formatSubtitleModeLabel(option.label, targetLanguageName)
                            : option.label}
                        </option>
                      ))}
                    </NativeSelect>
                  ) : (
                    <div className={field.name.startsWith("local_") ? "localPathInput" : undefined}>
                    <input
                      id={field.name}
                      type={field.secret ? "password" : field.type ?? "text"}
                      value={field.secret ? secretValues[field.name] ?? "" : values[field.name] ?? ""}
                      placeholder={field.secret && storedSecrets[field.name] ? t("settings.secretStored") : ""}
                      onChange={(event) => field.secret
                        ? (setSecretValues((current) => ({ ...current, [field.name]: event.target.value })),
                          setClearSecrets((current) => current.filter((name) => name !== field.name)))
                        : setValues((current) => ({ ...current, [field.name]: event.target.value }))}
                      disabled={loading || saving || clearSecrets.includes(field.name)}
                      required={(!field.optional || setup) && (!field.secret || !storedSecrets[field.name])}
                      autoComplete={field.secret ? "new-password" : "off"}
                    />
                    {field.name.startsWith("local_") && <LocalFolderPicker
                      value={values[field.name] ?? ""}
                      label={t(`settings.fields.${field.labelKey}`)}
                      disabled={loading || saving}
                      onSelect={(path) => setValues((current) => ({ ...current, [field.name]: path }))}
                    />}
                    </div>
                  )}
                  {field.secret && storedSecrets[field.name] && (
                    <span className="secretState">
                      {t("settings.secretStored")}
                      <span><input
                        type="checkbox"
                        aria-label={t("settings.clearField", { field: t(`settings.fields.${field.labelKey}`) })}
                        checked={clearSecrets.includes(field.name)}
                        onChange={(event) => setClearSecrets((current) => event.target.checked
                          ? [...current, field.name]
                          : current.filter((name) => name !== field.name))}
                        disabled={loading || saving}
                      /> {t("settings.clear")}</span>
                    </span>
                  )}
                </div>
              ))}
              </div>
            </fieldset>;
          })}
          {setup && step === 4 && <div className="setupReview">
            {steps.slice(0, 4).map((name, index) => <section key={name}>
              <div className="setupReviewTitle"><h3>{t(`settings.sections.${name}`)}</h3><button type="button" className="textButton" disabled={saving} onClick={() => goToStep(index)}>{t("setup.edit")}</button></div>
              <dl>{SECTIONS.filter((section) => section.titleKey === name || (name === "storage" && section.titleKey === "webdav")).flatMap((section) => section.fields).filter((field) => fieldIsVisible(field, values)).map((field) => {
                const value = field.secret ? t("setup.credentialReady") : (field.options ?? (field.optionKey ? options[field.optionKey] : []))?.find((option) => option.value === values[field.name])?.label ?? values[field.name];
                return <div key={field.name}><dt>{t(`settings.fields.${field.labelKey}`)}</dt><dd>{field.name === "default_subtitle_mode" ? formatSubtitleModeLabel(value, targetLanguageName) : value || "—"}</dd></div>;
              })}</dl>
            </section>)}
            <p>{t("setup.servicesHint")}</p>
          </div>}
        </div>
        </div>
        <div className="settingsActions">
          <div aria-live="polite">
            {loading && <span>{t("settings.loading")}</span>}
            {!loading && !error && (setup ? <span>{t("setup.saveHint")}</span> : dirty
              ? <span className="unsaved">{t("settings.unsaved")}</span>
              : message
                ? <span className="success"><Check size={16} aria-hidden="true" />{message}</span>
                : <span>{t("settings.upToDate")}</span>)}
            {error && <span className="settingsError" role="alert">{error}</span>}
          </div>
          {setup && step > 0 && <button type="button" className="textButton" disabled={saving} onClick={() => goToStep(step - 1)}>{t("setup.back")}</button>}
          <button className="start" type="submit" disabled={loading || saving || (!setup && !dirty)}>
            {saving ? t("settings.saving") : setup ? t(step === 4 ? "setup.finish" : "setup.next") : t("settings.save")}<ArrowRight size={16} strokeWidth={1.75} aria-hidden="true" />
          </button>
        </div>
      </form>
    </main>
  );
}
