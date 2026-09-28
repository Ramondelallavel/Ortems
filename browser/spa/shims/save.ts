// Files go through the host's download confirmation (the artifact frame blocks plain downloads).
declare global {
  interface Window {
    claude?: { use: (name: string) => Promise<any> };
  }
}

export async function saveBlob(blob: Blob, filename: string): Promise<void> {
  const downloads = window.claude ? await window.claude.use("downloads").catch(() => null) : null;
  if (!downloads) {
    const es = typeof navigator !== "undefined" && navigator.language.toLowerCase().startsWith("es");
    throw new Error(es ? `Las descargas no están disponibles en esta vista; ${filename} no se ha guardado.` : `Downloads are not available in this view, so ${filename} was not saved.`);
  }
  try {
    await downloads.save({ filename, data: blob });
  } catch (e: any) {
    if (e?.code === "declined") return;
    throw new Error(e?.message || "The file could not be saved.");
  }
}
