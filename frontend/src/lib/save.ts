// Hands a generated file to the user. The embedded build substitutes the host's download API.

export async function saveBlob(blob: Blob, filename: string): Promise<void> {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
