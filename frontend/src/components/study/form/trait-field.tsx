"use client";

import { useMemo, useState } from "react";
import {
  Command,
  CommandGroup,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverAnchor,
  PopoverContent,
} from "@/components/ui/popover";

export function TraitField({
  id,
  value,
  onChange,
  onBlur,
  suggestions,
  invalid,
  describedBy,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  onBlur: () => void;
  suggestions: string[];
  invalid: boolean;
  describedBy?: string;
}) {
  const [open, setOpen] = useState(false);
  const matches = useMemo(() => {
    const query = value.trim().toLowerCase();
    return suggestions.filter(
      (item) => item.toLowerCase().includes(query) && item !== value,
    );
  }, [suggestions, value]);

  return (
    <Popover
      open={open && matches.length > 0}
      onOpenChange={setOpen}
    >
      <PopoverAnchor asChild>
        <Input
          id={id}
          value={value}
          role="combobox"
          aria-expanded={open && matches.length > 0}
          aria-autocomplete="list"
          aria-invalid={invalid || undefined}
          aria-describedby={describedBy}
          autoComplete="off"
          placeholder="e.g. plant height"
          onFocus={() => setOpen(true)}
          onBlur={onBlur}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              setOpen(false);
            }
          }}
          onChange={(event) => {
            onChange(event.target.value);
            setOpen(true);
          }}
        />
      </PopoverAnchor>
      <PopoverContent
        align="start"
        className="w-72 p-0"
        onOpenAutoFocus={(event) => event.preventDefault()}
        onInteractOutside={(event) => {
          if (
            event.target instanceof Node &&
            event.target === document.getElementById(id)
          ) {
            event.preventDefault();
          }
        }}
      >
        <Command shouldFilter={false}>
          <CommandList>
            <CommandGroup heading="Suggestions">
              {matches.map((item) => (
                <CommandItem
                  key={item}
                  value={item}
                  onSelect={() => {
                    onChange(item);
                    setOpen(false);
                  }}
                >
                  {item}
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
