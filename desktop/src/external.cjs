function externalUrl(value) {
  if (typeof value !== "string" || value.length > 8192) throw new Error("EXTERNAL_URL_INVALID");
  const url = new URL(value);
  if (url.protocol !== "https:" || !url.hostname || url.username || url.password) {
    throw new Error("EXTERNAL_URL_INVALID");
  }
  return url.href;
}

module.exports = { externalUrl };
