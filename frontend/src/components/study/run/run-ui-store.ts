import { create } from "zustand";

type RunUi = {
  highlighted: string[];
  scrollTarget: { eventId: string; nonce: number } | null;
  highlight: (agentIds: string[]) => void;
  clearHighlight: () => void;
  showDispatch: (eventId: string) => void;
};

export const useRunUi = create<RunUi>()((set) => ({
  highlighted: [],
  scrollTarget: null,
  highlight: (agentIds) => set({ highlighted: agentIds }),
  clearHighlight: () => set({ highlighted: [] }),
  showDispatch: (eventId) =>
    set((state) => ({
      scrollTarget: {
        eventId,
        nonce: (state.scrollTarget?.nonce ?? 0) + 1,
      },
    })),
}));
