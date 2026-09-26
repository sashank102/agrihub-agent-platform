import { ContentBlock } from "@langchain/core/messages";
import { toast } from "sonner";

/** The server rejects HTTP bodies above this size. It stays authoritative. */
export const SERVER_BODY_LIMIT_BYTES = 10_485_760;
const JSON_OVERHEAD_BYTES = 256 * 1024;

export function base64EncodedBytes(byteLength: number): number {
  if (byteLength <= 0) {
    return 0;
  }
  return Math.ceil(byteLength / 3) * 4;
}

export function fileFitsRequestBudget(
  byteLength: number,
  alreadyEncodedBytes = 0,
): boolean {
  return (
    alreadyEncodedBytes + base64EncodedBytes(byteLength) <=
    SERVER_BODY_LIMIT_BYTES - JSON_OVERHEAD_BYTES
  );
}

export function uploadBudgetMessage(fileName: string): string {
  return `${fileName} is too large to send. Base64 encoding would exceed the server's 10 MB request limit.`;
}

// Returns a Promise of a typed multimodal block for images or PDFs
export async function fileToContentBlock(
  file: File,
): Promise<ContentBlock.Multimodal.Data> {
  const supportedImageTypes = [
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
  ];
  const supportedFileTypes = [...supportedImageTypes, "application/pdf"];

  if (!supportedFileTypes.includes(file.type)) {
    toast.error(
      `Unsupported file type: ${file.type}. Supported types are: ${supportedFileTypes.join(", ")}`,
    );
    return Promise.reject(new Error(`Unsupported file type: ${file.type}`));
  }

  if (!fileFitsRequestBudget(file.size)) {
    const message = uploadBudgetMessage(file.name);
    toast.error(message);
    return Promise.reject(new Error(message));
  }

  const data = await fileToBase64(file);

  if (supportedImageTypes.includes(file.type)) {
    return {
      type: "image",
      mimeType: file.type,
      data,
      metadata: { name: file.name },
    };
  }

  // PDF
  return {
    type: "file",
    mimeType: "application/pdf",
    data,
    metadata: { filename: file.name },
  };
}

// Helper to convert File to base64 string
export async function fileToBase64(file: File): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => {
      const result = reader.result as string;
      // Remove the data:...;base64, prefix
      resolve(result.split(",")[1]);
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

// Type guard for Base64ContentBlock
export function isBase64ContentBlock(
  block: unknown,
): block is ContentBlock.Multimodal.Data {
  if (typeof block !== "object" || block === null || !("type" in block))
    return false;
  // file type (legacy)
  if (
    (block as { type: unknown }).type === "file" &&
    "mimeType" in block &&
    typeof (block as { mimeType?: unknown }).mimeType === "string" &&
    ((block as { mimeType: string }).mimeType.startsWith("image/") ||
      (block as { mimeType: string }).mimeType === "application/pdf")
  ) {
    return true;
  }
  // image type (new)
  if (
    (block as { type: unknown }).type === "image" &&
    "mimeType" in block &&
    typeof (block as { mimeType?: unknown }).mimeType === "string" &&
    (block as { mimeType: string }).mimeType.startsWith("image/")
  ) {
    return true;
  }
  return false;
}
