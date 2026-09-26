import { useState, useRef, useEffect, useCallback, ChangeEvent } from "react";
import { toast } from "sonner";
import { ContentBlock } from "@langchain/core/messages";
import {
  fileFitsRequestBudget,
  fileToContentBlock,
  uploadBudgetMessage,
} from "@/lib/multimodal-utils";

export const SUPPORTED_FILE_TYPES = [
  "image/jpeg",
  "image/png",
  "image/gif",
  "image/webp",
  "application/pdf",
];

interface UseFileUploadOptions {
  initialBlocks?: ContentBlock.Multimodal.Data[];
}

export function useFileUpload({
  initialBlocks = [],
}: UseFileUploadOptions = {}) {
  const [contentBlocks, setContentBlocks] =
    useState<ContentBlock.Multimodal.Data[]>(initialBlocks);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const dropRef = useRef<HTMLDivElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const dragCounter = useRef(0);

  const isDuplicate = (file: File, blocks: ContentBlock.Multimodal.Data[]) => {
    if (file.type === "application/pdf") {
      return blocks.some(
        (b) =>
          b.type === "file" &&
          b.mimeType === "application/pdf" &&
          b.metadata?.filename === file.name,
      );
    }
    if (SUPPORTED_FILE_TYPES.includes(file.type)) {
      return blocks.some(
        (b) =>
          b.type === "image" &&
          b.metadata?.name === file.name &&
          b.mimeType === file.type,
      );
    }
    return false;
  };

  const acceptFiles = useCallback(async (files: File[]) => {
    const encoded = contentBlocks.reduce(
      (total, block) => total + (block.data?.length ?? 0),
      0,
    );
    const accepted: File[] = [];
    const rejected: string[] = [];
    let used = encoded;
    for (const file of files) {
      if (!fileFitsRequestBudget(file.size, used)) {
        rejected.push(file.name);
        continue;
      }
      used += Math.ceil(file.size / 3) * 4;
      accepted.push(file);
    }
    if (rejected.length > 0) {
      const message = uploadBudgetMessage(rejected.join(", "));
      setUploadError(message);
      toast.error(message);
    } else {
      setUploadError(null);
    }
    if (accepted.length === 0) {
      return;
    }
    const newBlocks = await Promise.all(accepted.map(fileToContentBlock));
    setContentBlocks((prev) => [...prev, ...newBlocks]);
  }, [contentBlocks]);

  const handleFileUpload = async (e: ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files) return;
    const fileArray = Array.from(files);
    const validFiles = fileArray.filter((file) =>
      SUPPORTED_FILE_TYPES.includes(file.type),
    );
    const invalidFiles = fileArray.filter(
      (file) => !SUPPORTED_FILE_TYPES.includes(file.type),
    );
    const duplicateFiles = validFiles.filter((file) =>
      isDuplicate(file, contentBlocks),
    );
    const uniqueFiles = validFiles.filter(
      (file) => !isDuplicate(file, contentBlocks),
    );

    if (invalidFiles.length > 0) {
      toast.error(
        "You have uploaded invalid file type. Please upload a JPEG, PNG, GIF, WEBP image or a PDF.",
      );
    }
    if (duplicateFiles.length > 0) {
      toast.error(
        `Duplicate file(s) detected: ${duplicateFiles.map((f) => f.name).join(", ")}. Each file can only be uploaded once per message.`,
      );
    }

    await acceptFiles(uniqueFiles);
    e.target.value = "";
  };

  // Drag and drop handlers
  useEffect(() => {
    if (!dropRef.current) return;

    // Global drag events with counter for robust dragOver state
    const handleWindowDragEnter = (e: DragEvent) => {
      if (e.dataTransfer?.types?.includes("Files")) {
        dragCounter.current += 1;
        setDragOver(true);
      }
    };
    const handleWindowDragLeave = (e: DragEvent) => {
      if (e.dataTransfer?.types?.includes("Files")) {
        dragCounter.current -= 1;
        if (dragCounter.current <= 0) {
          setDragOver(false);
          dragCounter.current = 0;
        }
      }
    };
    const handleWindowDrop = async (e: DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      dragCounter.current = 0;
      setDragOver(false);

      if (!e.dataTransfer) return;

      const files = Array.from(e.dataTransfer.files);
      const validFiles = files.filter((file) =>
        SUPPORTED_FILE_TYPES.includes(file.type),
      );
      const invalidFiles = files.filter(
        (file) => !SUPPORTED_FILE_TYPES.includes(file.type),
      );
      const duplicateFiles = validFiles.filter((file) =>
        isDuplicate(file, contentBlocks),
      );
      const uniqueFiles = validFiles.filter(
        (file) => !isDuplicate(file, contentBlocks),
      );

      if (invalidFiles.length > 0) {
        toast.error(
          "You have uploaded invalid file type. Please upload a JPEG, PNG, GIF, WEBP image or a PDF.",
        );
      }
      if (duplicateFiles.length > 0) {
        toast.error(
          `Duplicate file(s) detected: ${duplicateFiles.map((f) => f.name).join(", ")}. Each file can only be uploaded once per message.`,
        );
      }

      await acceptFiles(uniqueFiles);
    };
    const handleWindowDragEnd = (e: DragEvent) => {
      dragCounter.current = 0;
      setDragOver(false);
    };
    window.addEventListener("dragenter", handleWindowDragEnter);
    window.addEventListener("dragleave", handleWindowDragLeave);
    window.addEventListener("drop", handleWindowDrop);
    window.addEventListener("dragend", handleWindowDragEnd);

    // Prevent default browser behavior for dragover globally
    const handleWindowDragOver = (e: DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
    };
    window.addEventListener("dragover", handleWindowDragOver);

    // Remove element-specific drop event (handled globally)
    const handleDragOver = (e: DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setDragOver(true);
    };
    const handleDragEnter = (e: DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setDragOver(true);
    };
    const handleDragLeave = (e: DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setDragOver(false);
    };
    const element = dropRef.current;
    element.addEventListener("dragover", handleDragOver);
    element.addEventListener("dragenter", handleDragEnter);
    element.addEventListener("dragleave", handleDragLeave);

    return () => {
      element.removeEventListener("dragover", handleDragOver);
      element.removeEventListener("dragenter", handleDragEnter);
      element.removeEventListener("dragleave", handleDragLeave);
      window.removeEventListener("dragenter", handleWindowDragEnter);
      window.removeEventListener("dragleave", handleWindowDragLeave);
      window.removeEventListener("drop", handleWindowDrop);
      window.removeEventListener("dragend", handleWindowDragEnd);
      window.removeEventListener("dragover", handleWindowDragOver);
      dragCounter.current = 0;
    };
  }, [acceptFiles, contentBlocks]);

  const removeBlock = (idx: number) => {
    setContentBlocks((prev) => prev.filter((_, i) => i !== idx));
  };

  const resetBlocks = () => setContentBlocks([]);

  /**
   * Handle paste event for files (images, PDFs)
   * Can be used as onPaste={handlePaste} on a textarea or input
   */
  const handlePaste = async (
    e: React.ClipboardEvent<HTMLTextAreaElement | HTMLInputElement>,
  ) => {
    const items = e.clipboardData.items;
    if (!items) return;
    const files: File[] = [];
    for (let i = 0; i < items.length; i += 1) {
      const item = items[i];
      if (item.kind === "file") {
        const file = item.getAsFile();
        if (file) files.push(file);
      }
    }
    if (files.length === 0) {
      return;
    }
    e.preventDefault();
    const validFiles = files.filter((file) =>
      SUPPORTED_FILE_TYPES.includes(file.type),
    );
    const invalidFiles = files.filter(
      (file) => !SUPPORTED_FILE_TYPES.includes(file.type),
    );
    const isDuplicate = (file: File) => {
      if (file.type === "application/pdf") {
        return contentBlocks.some(
          (b) =>
            b.type === "file" &&
            b.mimeType === "application/pdf" &&
            b.metadata?.filename === file.name,
        );
      }
      if (SUPPORTED_FILE_TYPES.includes(file.type)) {
        return contentBlocks.some(
          (b) =>
            b.type === "image" &&
            b.metadata?.name === file.name &&
            b.mimeType === file.type,
        );
      }
      return false;
    };
    const duplicateFiles = validFiles.filter(isDuplicate);
    const uniqueFiles = validFiles.filter((file) => !isDuplicate(file));
    if (invalidFiles.length > 0) {
      toast.error(
        "You have pasted an invalid file type. Please paste a JPEG, PNG, GIF, WEBP image or a PDF.",
      );
    }
    if (duplicateFiles.length > 0) {
      toast.error(
        `Duplicate file(s) detected: ${duplicateFiles.map((f) => f.name).join(", ")}. Each file can only be uploaded once per message.`,
      );
    }
    if (uniqueFiles.length > 0) {
      await acceptFiles(uniqueFiles);
    }
  };

  return {
    contentBlocks,
    uploadError,
    setContentBlocks,
    handleFileUpload,
    dropRef,
    removeBlock,
    resetBlocks,
    dragOver,
    handlePaste,
  };
}
