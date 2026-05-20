
    const DEFAULT_ZOOM_SECONDS = 18;
    const MIN_ZOOM_SECONDS = 0.35;
    const MIN_WORD_ORDER_GAP = 0.001;
    const PLAYHEAD_DRAG_SENSITIVITY = 0.2;
    const LYRICS_SOURCE_DEFAULT_URL = "https://shirrim.com/singers/israel-singers/";
    const LYRICS_TEXT_DEBOUNCE_MS = 500;

    const $ = (id) => document.getElementById(id);

    const ui = {
      youtubeUrl: $("youtubeUrl"),
      lyricsSourceLink: $("lyricsSourceLink"),
      audioFile: $("audioFile"),
      lyricsText: $("lyricsText"),
      projectName: $("projectName"),
      projectSelect: $("projectSelect"),
      importProject: $("importProject"),
      aiFirstPass: $("aiFirstPass"),
      stopAiPass: $("stopAiPass"),
      saveProject: $("saveProject"),
      deleteProject: $("deleteProject"),
      projectStatus: $("projectStatus"),
      pathsStatus: $("pathsStatus"),
      pipelineStatus: $("pipelineStatus"),
      pipelineStage: $("pipelineStage"),
      pipelineElapsed: $("pipelineElapsed"),
      pipelineProgressFill: $("pipelineProgressFill"),
      pipelineDetail: $("pipelineDetail"),
      pipelineStages: $("pipelineStages"),
      preview: $("preview"),
      selectedWordLabel: $("selectedWordLabel"),
      zoomLabel: $("zoomLabel"),
      audioTime: $("audioTime"),
      zoomOut: $("zoomOut"),
      zoomIn: $("zoomIn"),
      zoomReset: $("zoomReset"),
      prevWord: $("prevWord"),
      nextWord: $("nextWord"),
      lastWord: $("lastWord"),
      placeWord: $("placeWord"),
      waveShell: $("waveShell"),
      wordLayer: $("wordLayer"),
      waveCanvas: $("waveCanvas"),
      wavePan: $("wavePan"),
      wavePanStart: $("wavePanStart"),
      wavePanEnd: $("wavePanEnd"),
      playhead: $("playhead"),
      audio: $("audio"),
      vocalsAudio: $("vocalsAudio"),
      musicAudio: $("musicAudio"),
      rewindStart: $("rewindStart"),
      playPause: $("playPause"),
      back2: $("back2"),
      jumpWord: $("jumpWord"),
      exportMp4: $("exportMp4"),
      exportStatus: $("exportStatus"),
      exportStage: $("exportStage"),
      exportPercent: $("exportPercent"),
      exportProgressFill: $("exportProgressFill"),
      exportDetail: $("exportDetail"),
      resetWord: $("resetWord"),
      resetAll: $("resetAll"),
      stemMix: $("stemMix"),
    };

    const state = {
      session: null,
      projects: [],
      manifest: { lines: [], words: [] },
      overrides: { version: 1, global_offset: 0, placed_word_count: 0, lines: {}, words: {} },
      waveform: null,
      baseWords: [],
      resolvedWords: [],
      selectedWordIndex: 0,
      placedWordCount: 0,
      zoomSeconds: DEFAULT_ZOOM_SECONDS,
      displayWindow: { start: 0, end: DEFAULT_ZOOM_SECONDS },
      playheadDrag: null,
      wordDrag: null,
      redrawQueued: false,
      exportPoller: null,
      pipelinePoller: null,
      pipelineRunning: false,
      pipelineAwaitingProjectLoad: false,
      pipelineLoadingProject: false,
      exportLocalStartMs: null,
      outputVideoPath: null,
      lyricsTextDebounce: null,
      stemMixValue: 0,
      stemMixAvailable: false,
    };

    function clamp(value, min, max) {
      return Math.min(Math.max(value, min), max);
    }

    function hasProject() {
      return state.baseWords.length > 0 && !!state.waveform;
    }

    function formatClock(seconds) {
      const safe = Number.isFinite(seconds) ? Math.max(seconds, 0) : 0;
      const mins = Math.floor(safe / 60);
      const secs = Math.floor(safe % 60);
      const hundredths = Math.floor((safe - Math.floor(safe)) * 100);
      return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}.${String(hundredths).padStart(2, "0")}`;
    }

    function formatPlaybackTime(seconds) {
      const safe = Number.isFinite(seconds) ? Math.max(seconds, 0) : 0;
      const mins = Math.floor(safe / 60);
      const secs = Math.floor(safe % 60);
      return `${mins}:${String(secs).padStart(2, "0")}`;
    }

    function inferDirection(text) {
      return /[\u0590-\u05FF\u0600-\u06FF]/.test(text) ? "rtl" : "ltr";
    }

    function getAudioDuration() {
      if (Number.isFinite(ui.audio.duration) && ui.audio.duration > 0) {
        return ui.audio.duration;
      }
      if (state.waveform && Number.isFinite(state.waveform.duration)) {
        return state.waveform.duration;
      }
      const lastResolved = [...state.resolvedWords].reverse().find((word) => Number.isFinite(word.end));
      if (lastResolved) {
        return lastResolved.end;
      }
      const lastBase = state.baseWords[state.baseWords.length - 1];
      return lastBase ? lastBase.end : 0;
    }

    function clampUnit(value) {
      return clamp(value, -1, 1);
    }

    function applyStemMix() {
      const safe = clampUnit(state.stemMixValue);
      const deadZone = 0.08;
      const absSafe = Math.abs(safe);
      let masterGain = 0.0;
      let vocalsGain = 0.0;
      let musicGain = 0.0;
      if (absSafe <= deadZone) {
        masterGain = 1.0;
      } else if (safe < -deadZone) {
        vocalsGain = clamp((-safe - deadZone) / (1 - deadZone), 0, 1);
        masterGain = 1.0 - vocalsGain;
      } else {
        musicGain = clamp((safe - deadZone) / (1 - deadZone), 0, 1);
        masterGain = 1.0 - musicGain;
      }
      if (state.stemMixAvailable) {
        ui.audio.muted = false;
        ui.audio.volume = masterGain;
        ui.vocalsAudio.volume = vocalsGain;
        ui.musicAudio.volume = musicGain;
      } else {
        ui.audio.muted = false;
        ui.audio.volume = 1.0;
      }
    }

    function pauseStemPlayers() {
      ui.vocalsAudio.pause();
      ui.musicAudio.pause();
    }

    function syncStemPlayback(options = {}) {
      if (!state.stemMixAvailable) {
        return;
      }
      const force = Boolean(options.force);
      const targetTime = clamp(ui.audio.currentTime || 0, 0, getAudioDuration());
      [ui.vocalsAudio, ui.musicAudio].forEach((player) => {
        const current = Number.isFinite(player.currentTime) ? player.currentTime : 0;
        if (force || Math.abs(current - targetTime) > 0.08) {
          try {
            player.currentTime = targetTime;
          } catch (_) {
            // ignore media seek sync errors
          }
        }
        if (ui.audio.paused) {
          player.pause();
        } else if (player.paused) {
          player.play().catch(() => {});
        }
      });
    }

    function refreshStemAvailability() {
      const vocalsReady = ui.vocalsAudio.readyState >= 1;
      const musicReady = ui.musicAudio.readyState >= 1;
      state.stemMixAvailable = vocalsReady && musicReady;
      applyStemMix();
      renderButtons();
    }

    function setupStemPlaybackSources() {
      const seed = Date.now();
      ui.audio.src = `/api/audio?artifact=audio_wav&t=${seed}`;
      ui.vocalsAudio.src = `/api/audio?artifact=vocals_wav&t=${seed}`;
      ui.musicAudio.src = `/api/audio?artifact=no_vocals_wav&t=${seed}`;
      ui.audio.load();
      ui.vocalsAudio.load();
      ui.musicAudio.load();
      state.stemMixAvailable = false;
      applyStemMix();
      
      // Re-check availability once audio metadata loads
      const onReady = () => refreshStemAvailability();
      ui.vocalsAudio.addEventListener("loadedmetadata", onReady, { once: true });
      ui.musicAudio.addEventListener("loadedmetadata", onReady, { once: true });
    }

    function getWordDuration(baseWord) {
      const duration = baseWord.end - baseWord.start;
      return duration > 0.01 ? duration : 0.12;
    }

    function syncResolvedWords() {
      const overrides = state.overrides.words || {};
      const manifestWords = Array.isArray(state.manifest.words) ? state.manifest.words : [];
      const manifestLines = Array.isArray(state.manifest.lines) ? state.manifest.lines : [];

      // Create a set of word IDs that are the last words of any line
      const lineEndWordIds = new Set();
      manifestLines.forEach((line) => {
        if (Array.isArray(line.word_ids) && line.word_ids.length > 0) {
          lineEndWordIds.add(line.word_ids[line.word_ids.length - 1]);
        }
      });

      state.baseWords = [...manifestWords].sort((a, b) => a.index - b.index);
      state.placedWordCount = clamp(Number(state.overrides.placed_word_count || 0), 0, state.baseWords.length);

      // Pass 1: Resolve starts and base ends
      const words = state.baseWords.map((word, index) => {
        const override = overrides[word.id] || {};
        const duration = getWordDuration(word);
        const isCommitted = index < state.placedWordCount;
        const start = isCommitted
          ? (Number.isFinite(override.start) ? override.start : (Number.isFinite(word.start) ? word.start : 0))
          : null;
        let end = isCommitted
          ? (Number.isFinite(override.end) ? override.end : word.end)
          : null;

        return {
          ...word,
          duration,
          start,
          // Vocal end MUST be based on the natural duration to detect gaps correctly.
          // Using override.end here would cause circular logic since override.end 
          // might already be 'chained' to the next word.
          vocalEnd: Number.isFinite(start) ? start + duration : null,
          isLineEnd: lineEndWordIds.has(word.id),
        };
      });

      // Pass 2: Refine end times based on the "Chained Timing" and "2s Padding" rules
      state.resolvedWords = words.map((word, index) => {
        if (word.start === null) return word;

        const nextWord = words[index + 1];
        const isLastWord = index === words.length - 1;
        const isNextPlaced = nextWord && nextWord.start !== null;
        
        // Cap natural vocal duration at 2.0 seconds to prevent tails extending into silence
        const vocalEndCapped = word.start + Math.min(word.duration, 2.0);
        let finalEnd = vocalEndCapped;

        if (isLastWord) {
          // Rule: Last word of song gets 2.0s padding
          finalEnd = vocalEndCapped + 2.0;
        } else if (isNextPlaced) {
          const nextStart = nextWord.start;
          const gap = nextStart - vocalEndCapped;

          if (word.isLineEnd || gap > 2.0) {
            // Rule: End of line or large gap gets 2.0s padding
            finalEnd = Math.min(vocalEndCapped + 2.0, nextStart);
          } else {
            // Rule: Chained presentation
            finalEnd = nextStart;
          }
        } else {
          if (word.isLineEnd) {
            finalEnd = vocalEndCapped + 2.0;
          }
        }

        return {
          ...word,
          end: Number(finalEnd.toFixed(3)),
        };
      });

      if (!state.wordDrag) {
        const currentIndex = Math.min(state.placedWordCount, Math.max(state.resolvedWords.length - 1, 0));
        state.selectedWordIndex = clamp(currentIndex, 0, Math.max(state.resolvedWords.length - 1, 0));
      }
    }

    function getResolvedLines() {
      const wordById = new Map(state.resolvedWords.map((word) => [word.id, word]));
      const baseWordById = new Map(state.baseWords.map((word) => [word.id, word]));
      const manifestLines = Array.isArray(state.manifest.lines) ? state.manifest.lines : [];
      return manifestLines.map((line) => {
        const allWords = (line.word_ids || []).map((wordId) => baseWordById.get(wordId)).filter(Boolean);
        const words = (line.word_ids || [])
          .map((wordId) => wordById.get(wordId))
          .filter((word) => word && Number.isFinite(word.start) && Number.isFinite(word.end));
        if (!words.length) {
          return { ...line, allWords, words: [], start: line.start || 0, end: line.end || 0 };
        }
        return {
          ...line,
          allWords,
          words,
          start: words[0].start,
          end: words[words.length - 1].end,
        };
      });
    }

    function getSelectedWord() {
      return state.resolvedWords[state.selectedWordIndex] || null;
    }

    function updateStatusChip(element, text, mode) {
      element.textContent = text;
      element.className = "chip";
      if (mode) {
        element.classList.add(mode);
      }
    }

    function renderSelectedWordLabel(selectedWord) {
      const wordText = selectedWord ? selectedWord.text : "none";
      const selectedPosition = selectedWord ? selectedWord.index + 1 : 0;
      ui.selectedWordLabel.innerHTML = "";
      const wordSpan = document.createElement("span");
      wordSpan.className = "selected-word-text";
      wordSpan.textContent = wordText;
      const spacer = document.createTextNode(" ");
      const countSpan = document.createElement("span");
      countSpan.className = "selected-word-count";
      countSpan.textContent = `${selectedPosition}/${state.resolvedWords.length}`;
      ui.selectedWordLabel.append(wordSpan, spacer, countSpan);
    }

    function displayOutputName(rawPath) {
      if (!rawPath) {
        return "No output yet.";
      }
      const normalized = String(rawPath).split(/[\\/]/).filter(Boolean);
      return normalized.length ? normalized[normalized.length - 1] : String(rawPath);
    }

    function deriveLyricsTextFromManifest() {
      if (!Array.isArray(state.manifest.lines)) {
        return "";
      }
      const lines = state.manifest.lines
        .map((line) => (line && typeof line.text === "string" ? line.text.trim() : ""))
        .filter(Boolean);
      return lines.join("\n");
    }

    function splitLyricWords(text) {
      return String(text || "").trim().split(/\s+/).filter(Boolean);
    }

    function cleanLyricsLines(rawText) {
      return String(rawText || "")
        .split(/\r?\n/)
        .map((line) => line.replace(/\s+/g, " ").trim())
        .filter(Boolean);
    }

    function buildManifestFromLyricsText(lyricsText, durationSeconds) {
      const lines = cleanLyricsLines(lyricsText);
      const flattened = [];
      lines.forEach((line, lineIndex) => {
        splitLyricWords(line).forEach((token) => {
          flattened.push({ lineIndex, token });
        });
      });
      if (!flattened.length) {
        return { lines: [], words: [] };
      }
      
      const safeDuration = Number.isFinite(durationSeconds) && durationSeconds > 0 
        ? durationSeconds 
        : Math.max(flattened.length * 0.5, 10.0);
        
      const slotDuration = safeDuration / flattened.length;
      const words = [];
      const lineToWordIds = new Map(lines.map((_, index) => [index, []]));

      flattened.forEach((entry, index) => {        const id = `word_${String(index).padStart(4, "0")}`;
        lineToWordIds.get(entry.lineIndex).push(id);
        words.push({
          id,
          index,
          line_index: entry.lineIndex,
          text: entry.token,
          start: null, // No default timing to avoid "mounted" stacking
          end: null,
        });
      });

      const wordLookup = new Map(words.map((word) => [word.id, word]));
      const manifestLines = lines.map((line, index) => {
        const wordIds = lineToWordIds.get(index) || [];
        const start = wordIds.length ? Number(wordLookup.get(wordIds[0]).start) : 0;
        const end = wordIds.length ? Number(wordLookup.get(wordIds[wordIds.length - 1]).end) : 0.01;
        return {
          id: `line_${String(index).padStart(3, "0")}`,
          index,
          text: line,
          start,
          end,
          word_ids: wordIds,
        };
      });

      return { lines: manifestLines, words };
    }

    function updateLyricsSourceLink(url) {
      if (!ui.lyricsSourceLink) {
        return;
      }
      const safeUrl = String(url || "").trim() || LYRICS_SOURCE_DEFAULT_URL;
      ui.lyricsSourceLink.href = safeUrl;
      ui.lyricsSourceLink.textContent = safeUrl;
    }

    function applyLyricsTextLocally(rawLyricsText) {
      const nextManifest = buildManifestFromLyricsText(rawLyricsText, getAudioDuration());
      const previousPlaced = state.placedWordCount;
      const oldCommitted = state.baseWords.slice(0, previousPlaced);
      let preservedCount = 0;
      while (
        preservedCount < oldCommitted.length
        && preservedCount < nextManifest.words.length
        && oldCommitted[preservedCount].text === nextManifest.words[preservedCount].text
      ) {
        preservedCount += 1;
      }

      const preservedOverrides = {};
      for (let index = 0; index < preservedCount; index += 1) {
        const oldWord = state.resolvedWords[index];
        const newWord = nextManifest.words[index];
        if (!oldWord || !newWord || !Number.isFinite(oldWord.start) || !Number.isFinite(oldWord.end)) {
          continue;
        }
        preservedOverrides[newWord.id] = {
          start: Number(oldWord.start.toFixed(3)),
          end: Number(oldWord.end.toFixed(3)),
        };
      }

      state.manifest = nextManifest;
      state.overrides.words = preservedOverrides;
      state.overrides.lines = {};
      state.overrides.placed_word_count = preservedCount;
      state.overrides.lyrics_text = cleanLyricsLines(rawLyricsText).join("\n");
      state.placedWordCount = preservedCount;
      if (state.session) {
        state.session.lyrics_text = state.overrides.lyrics_text;
      }
      syncResolvedWords();
      renderAll();
      if (state.waveform) {
        updateStatusChip(ui.projectStatus, `Ready: ${state.resolvedWords.length} words`, state.resolvedWords.length ? "ok" : undefined);
      }
    }

    function scheduleLyricsTextRebuild() {
      if (state.lyricsTextDebounce) {
        clearTimeout(state.lyricsTextDebounce);
      }
      state.lyricsTextDebounce = setTimeout(() => {
        state.lyricsTextDebounce = null;
        applyLyricsTextLocally(ui.lyricsText.value);
      }, LYRICS_TEXT_DEBOUNCE_MS);
    }

    function renderProjectSelect(currentProjectId) {
      if (!ui.projectSelect) {
        return;
      }
      const selectedId = currentProjectId || state.session?.project_id || "";
      ui.projectSelect.innerHTML = "";
      if (!state.projects.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "No saved projects";
        ui.projectSelect.appendChild(option);
        return;
      }
      state.projects.forEach((project) => {
        const option = document.createElement("option");
        option.value = project.id;
        option.textContent = project.name;
        option.selected = project.id === selectedId;
        ui.projectSelect.appendChild(option);
      });
    }

    function estimatedExportProgress(payload) {
      const rawProgress = Number(payload?.progress);
      const reportedProgress = Number.isFinite(rawProgress) ? Math.max(0, Math.min(rawProgress, 100)) : 0;
      if (!["queued", "building_subtitles", "rendering"].includes(payload?.status)) {
        return reportedProgress;
      }

      const parsedStartedAt = payload?.started_at ? Date.parse(payload.started_at) : NaN;
      const startedMs = Number.isFinite(parsedStartedAt) ? parsedStartedAt : state.exportLocalStartMs;
      const estimatedTotalSeconds = Number(payload?.estimated_total_seconds);
      if (!startedMs || !Number.isFinite(estimatedTotalSeconds) || estimatedTotalSeconds <= 0) {
        return reportedProgress;
      }

      const elapsedSeconds = Math.max((Date.now() - startedMs) / 1000, 0);
      const estimatedProgress = Math.min(Math.max(15 + (elapsedSeconds / estimatedTotalSeconds) * 75, 0), 90);
      return Math.max(reportedProgress, Math.round(estimatedProgress));
    }

    function renderExportStatus(payload) {
      const status = payload?.status || "idle";
      const detail = payload?.detail || payload?.status || "Idle";
      const progress = estimatedExportProgress(payload);

      if (status === "idle") {
        ui.exportStatus.hidden = true;
        ui.exportStage.textContent = "Idle";
        ui.exportPercent.textContent = "0%";
        ui.exportProgressFill.style.width = "0%";
        ui.exportDetail.textContent = "No export running.";
        state.outputVideoPath = null;
        state.exportLocalStartMs = null;
        return;
      }

      state.outputVideoPath = payload?.output_video || null;
      ui.exportStatus.hidden = false;
      ui.exportStage.textContent = detail;
      ui.exportPercent.textContent = `${progress}%`;
      ui.exportProgressFill.style.width = `${progress}%`;
      ui.exportDetail.textContent = payload?.error || payload?.output_video || `Status: ${status}`;
    }

    async function syncExportStatus() {
      const payload = await fetchJson("/api/export/status");
      if (payload?.started_at) {
        const parsedStartedAt = Date.parse(payload.started_at);
        if (Number.isFinite(parsedStartedAt)) {
          state.exportLocalStartMs = parsedStartedAt;
        }
      }
      renderExportStatus(payload);
      return payload;
    }

    function stopExportPolling() {
      if (state.exportPoller) {
        clearInterval(state.exportPoller);
        state.exportPoller = null;
      }
    }

    function startExportPolling() {
      stopExportPolling();
      state.exportPoller = setInterval(async () => {
        try {
          const payload = await syncExportStatus();
          if (!["queued", "building_subtitles", "rendering"].includes(payload.status)) {
            stopExportPolling();
            ui.exportMp4.disabled = false;
          }
        } catch (_) {
          stopExportPolling();
          ui.exportMp4.disabled = false;
        }
      }, 2000);
    }

    function formatPipelineElapsed(seconds) {
      const safe = Number.isFinite(seconds) ? Math.max(seconds, 0) : 0;
      return formatPlaybackTime(safe);
    }

    function describePipelineStageStatus(status) {
      switch (String(status || "pending")) {
        case "running":
          return "Running";
        case "done":
          return "Done";
        case "skipped":
          return "Skipped";
        case "error":
          return "Error";
        default:
          return "Pending";
      }
    }

    function pipelineStageFillWidth(stage) {
      switch (String(stage?.status || "pending")) {
        case "running":
          return 54;
        case "done":
        case "skipped":
        case "error":
          return 100;
        default:
          return 0;
      }
    }

    function renderPipelineStatus(payload) {
      if (!ui.pipelineStatus) {
        return;
      }

      const stages = Array.isArray(payload?.stages) ? payload.stages : [];
      const status = String(payload?.status || "idle");
      const currentStage = stages.find((stage) => stage && stage.status === "running") || null;
      const finishedCount = stages.filter((stage) => stage && ["done", "skipped"].includes(stage.status)).length;
      const totalStages = stages.length || 1;
      const progress = Math.max(0, Math.min((finishedCount / totalStages) * 100, 100));
      const elapsed = currentStage
        ? formatPipelineElapsed(currentStage.elapsed_seconds || payload?.stage_elapsed_seconds || 0)
        : (status === "completed" ? formatPipelineElapsed(payload?.stage_elapsed_seconds || 0) : "0:00");
      const summaryStatus = status === "running"
        ? `Running: ${currentStage?.label || payload?.current_stage_label || "Pipeline"}`
        : status === "completed"
          ? "Pipeline complete"
          : status === "error"
            ? "Pipeline failed"
            : "Pipeline idle";

      ui.pipelineStage.textContent = summaryStatus;
      ui.pipelineElapsed.textContent = elapsed;
      ui.pipelineProgressFill.style.width = `${progress}%`;
      ui.pipelineDetail.textContent = status === "running"
        ? `${finishedCount}/${stages.length} stages done`
        : status === "completed"
          ? `${finishedCount}/${stages.length} stages done`
          : status === "error"
            ? (payload?.error || "The pipeline stopped with an error.")
            : "No pipeline running.";

      ui.pipelineStages.innerHTML = "";
      state.pipelineRunning = status === "running";
      ui.pipelineStages.hidden = status !== "running";
      if (ui.stopAiPass) {
        ui.stopAiPass.hidden = status !== "running";
      }
      if (status !== "running") {
        return;
      }

      stages.forEach((stage) => {
        const row = document.createElement("div");
        const stageStatus = String(stage?.status || "pending");
        row.className = `pipeline-stage ${stageStatus}`;

        const head = document.createElement("div");
        head.className = "pipeline-stage-head";

        const name = document.createElement("div");
        name.className = "pipeline-stage-name";
        name.textContent = stage?.label || stage?.key || "Stage";

        const meta = document.createElement("div");
        meta.className = "pipeline-stage-meta";

        const spinner = document.createElement("span");
        spinner.className = "pipeline-spin";

        const badge = document.createElement("span");
        badge.className = `pipeline-stage-status ${stageStatus}`;
        badge.textContent = String(stageStatus || "pending").toUpperCase();

        const elapsedValue = document.createElement("span");
        elapsedValue.textContent = ["running", "done"].includes(stageStatus)
          ? formatPipelineElapsed(stage?.elapsed_seconds || 0)
          : "";

        meta.append(spinner, badge);
        if (elapsedValue.textContent) {
          meta.append(elapsedValue);
        }

        head.append(name, meta);

        const bar = document.createElement("div");
        bar.className = "pipeline-stage-bar";
        const fill = document.createElement("div");
        fill.className = `pipeline-stage-fill ${stageStatus}`;
        fill.style.width = `${pipelineStageFillWidth(stage)}%`;
        bar.appendChild(fill);

        row.append(head, bar);
        ui.pipelineStages.appendChild(row);
      });
    }

    async function syncPipelineStatus() {
      const payload = await fetchJson(`/api/pipeline/status?t=${Date.now()}`);
      renderPipelineStatus(payload);
      return payload;
    }

    function stopPipelinePolling() {
      if (state.pipelinePoller) {
        clearInterval(state.pipelinePoller);
        state.pipelinePoller = null;
      }
    }

    async function stopPipelineAndLoadProject() {
      state.pipelineAwaitingProjectLoad = false;
      stopPipelinePolling();
      await loadProject({ bustCache: true });
    }

    function startPipelinePolling() {
      stopPipelinePolling();
      state.pipelinePoller = setInterval(async () => {
        try {
          const payload = await syncPipelineStatus();
          if (payload.status === "running") {
            return;
          }
          stopPipelinePolling();
          if (payload.status === "completed" && state.pipelineAwaitingProjectLoad) {
            await stopPipelineAndLoadProject();
            updateStatusChip(
              ui.projectStatus,
              `Pipeline complete: ${payload.project_name || state.session?.project_name || "Project ready"}`,
              "ok",
            );
            return;
          }
          state.pipelineAwaitingProjectLoad = false;
          if (payload.status === "error") {
            updateStatusChip(ui.projectStatus, payload.error || "Pipeline failed.", "warn");
          }
        } catch (_) {
          stopPipelinePolling();
        }
      }, 1000);
    }

    async function runAiFirstPass() {
      if (!state.session?.project_id) {
        updateStatusChip(ui.projectStatus, "Load a project first.", "warn");
        return;
      }
      if (!hasProject()) {
        updateStatusChip(ui.projectStatus, "Load a project with audio first.", "warn");
        return;
      }

      ui.aiFirstPass.disabled = true;
      state.pipelineAwaitingProjectLoad = true;
      updateStatusChip(ui.projectStatus, "AI First Pass started. Watching progress...", "ok");

      try {
        const responsePayload = await fetchJson("/api/pipeline/first-pass", {
          method: "POST",
        });
        if (responsePayload.status === "running") {
          try {
            const pipelineStatus = await syncPipelineStatus();
            if (pipelineStatus.status === "running") {
              startPipelinePolling();
            } else if (pipelineStatus.status === "completed") {
              await stopPipelineAndLoadProject();
              updateStatusChip(ui.projectStatus, "AI First Pass complete.", "ok");
            }
          } catch (_) {
            startPipelinePolling();
          }
          return;
        }

        renderPipelineStatus(responsePayload);
        await stopPipelineAndLoadProject();
        updateStatusChip(ui.projectStatus, "AI First Pass complete.", "ok");
      } catch (error) {
        state.pipelineAwaitingProjectLoad = false;
        stopPipelinePolling();
        updateStatusChip(ui.projectStatus, error.message, "warn");
      } finally {
        ui.aiFirstPass.disabled = false;
      }
    }

    async function stopAiFirstPass() {
      if (ui.stopAiPass) {
        ui.stopAiPass.disabled = true;
      }
      try {
        await fetchJson("/api/pipeline/stop", {
          method: "POST",
        });
        state.pipelineAwaitingProjectLoad = false;
        stopPipelinePolling();
        renderPipelineStatus({ status: "idle", stages: [] });
        updateStatusChip(ui.projectStatus, "AI First Pass stopped.", "warn");
        renderButtons();
      } catch (error) {
        updateStatusChip(ui.projectStatus, error.message, "warn");
      } finally {
        if (ui.stopAiPass) {
          ui.stopAiPass.disabled = false;
        }
      }
    }

    function setSelectedWord(index, options = {}) {
      if (!state.resolvedWords.length) {
        state.selectedWordIndex = 0;
        renderAll();
        return;
      }
      const maxSelectable = state.placedWordCount >= state.resolvedWords.length
        ? Math.max(state.resolvedWords.length - 1, 0)
        : Math.min(state.placedWordCount, state.resolvedWords.length - 1);
      state.selectedWordIndex = clamp(index, 0, maxSelectable);
      if (options.seek) {
        const selected = getSelectedWord();
        if (selected && Number.isFinite(selected.start)) {
          setPlayheadTime(selected.start);
          setWindowAround(selected.start);
        }
      }
      renderAll();
    }

    function updateWindow() {
      const duration = getAudioDuration();
      const zoom = clamp(state.zoomSeconds, MIN_ZOOM_SECONDS, Math.max(duration || 18, MIN_ZOOM_SECONDS));
      state.zoomSeconds = zoom;
      const currentTime = clamp(ui.audio.currentTime || 0, 0, duration);
      let start = currentTime - zoom / 2;
      // Panning during drag is removed to prevent layout scrambling
      state.displayWindow = { start, end: start + zoom };
      if (ui.zoomLabel) {
        ui.zoomLabel.textContent = `Zoom ${zoom.toFixed(1)}s`;
      }
      if (ui.audioTime) {
        ui.audioTime.textContent = `${formatPlaybackTime(currentTime)} / ${formatPlaybackTime(duration)}`;
      }
      ui.wavePan.min = "0";
      ui.wavePan.max = String(duration);
      ui.wavePan.value = String(currentTime);
      if (ui.wavePanStart) {
        ui.wavePanStart.textContent = `Window ${formatClock(Math.max(start, 0))}`;
      }
      if (ui.wavePanEnd) {
        ui.wavePanEnd.textContent = formatClock(Math.min(start + zoom, duration));
      }
    }

    function setWindowAround(time) {
      state.displayWindow = { start: time - state.zoomSeconds / 2, end: time + state.zoomSeconds / 2 };
    }

    function setPlayheadTime(target) {
      const nextTime = clamp(target, 0, getAudioDuration());
      ui.audio.currentTime = nextTime;
    }

    function buildOverridesFromResolvedWords() {
      const words = {};
      state.resolvedWords.forEach((word, index) => {
        if (index >= state.placedWordCount) {
          return;
        }
        if (!Number.isFinite(word.start) || !Number.isFinite(word.end)) {
          return;
        }
        words[word.id] = {
          start: Number(word.start.toFixed(3)),
          end: Number(word.end.toFixed(3)),
        };
      });
      state.overrides.words = words;
      state.overrides.lines = {};
      state.overrides.placed_word_count = state.placedWordCount;
    }

    function setWordStart(index, targetStart) {
      if (!state.resolvedWords.length || index < 0 || index >= state.resolvedWords.length) {
        return false;
      }
      const nextWords = state.resolvedWords.map((word) => ({ ...word }));
      const current = nextWords[index];
      const previous = nextWords[index - 1] || null;
      const minStart = previous && Number.isFinite(previous.start) ? previous.start + MIN_WORD_ORDER_GAP : 0;
      const duration = getAudioDuration();
      const wordDuration = Math.max(current.duration || getWordDuration(state.baseWords[index] || current), 0.12);
      const clampedStart = clamp(targetStart, minStart, Math.max(duration - wordDuration, minStart));

      current.start = Number(clampedStart.toFixed(3));
      // End will be updated by syncResolvedWords after buildOverrides
      current.end = Number((clampedStart + wordDuration).toFixed(3));
      state.resolvedWords = nextWords;
      buildOverridesFromResolvedWords();
      syncResolvedWords(); // Trigger re-chaining
      return true;
    }

    function setDraggedWordStart(index, targetStart) {
      const committedCount = state.placedWordCount;
      if (!committedCount || index < 0 || index >= committedCount || index >= state.resolvedWords.length) {
        return false;
      }

      const duration = getAudioDuration();
      const nextWords = state.resolvedWords.map((word) => ({ ...word }));
      const starts = nextWords.map((word, wordIndex) => {
        if (wordIndex >= committedCount || !Number.isFinite(word.start)) {
          return null;
        }
        return state.wordDrag && state.wordDrag.originalStarts ? state.wordDrag.originalStarts[wordIndex] : word.start;
      });

      starts[index] = Math.max(targetStart, 0);

      for (let wordIndex = index - 1; wordIndex >= 0; wordIndex -= 1) {
        starts[wordIndex] = Math.min(starts[wordIndex], starts[wordIndex + 1] - MIN_WORD_ORDER_GAP);
      }

      if (starts[0] < 0) {
        const shiftRight = -starts[0];
        for (let wordIndex = 0; wordIndex <= index; wordIndex += 1) {
          starts[wordIndex] += shiftRight;
        }
      }

      for (let wordIndex = index + 1; wordIndex < committedCount; wordIndex += 1) {
        starts[wordIndex] = Math.max(starts[wordIndex], starts[wordIndex - 1] + MIN_WORD_ORDER_GAP);
      }

      if (Number.isFinite(duration) && duration > 0) {
        const lastWord = nextWords[committedCount - 1];
        const lastDuration = Math.max(lastWord.duration || getWordDuration(state.baseWords[committedCount - 1] || lastWord), 0.12);
        const maxLastStart = Math.max(duration - lastDuration, 0);
        const overflow = starts[committedCount - 1] - maxLastStart;
        if (overflow > 0) {
          for (let wordIndex = 0; wordIndex < committedCount; wordIndex += 1) {
            starts[wordIndex] -= overflow;
          }
          if (starts[0] < 0) {
            const shiftRight = -starts[0];
            for (let wordIndex = 0; wordIndex < committedCount; wordIndex += 1) {
              starts[wordIndex] += shiftRight;
            }
          }
        }
      }

      for (let wordIndex = 0; wordIndex < committedCount; wordIndex += 1) {
        nextWords[wordIndex].start = Number(starts[wordIndex].toFixed(3));
      }

      state.resolvedWords = nextWords;
      commitVisualStateToData();
      return true;
    }

    function commitVisualStateToData() {
      buildOverridesFromResolvedWords();
      syncResolvedWords();
      renderAll();
    }

    function commitCurrentWord(targetStart) {
      const currentIndex = state.selectedWordIndex;
      const wasAlreadyPlaced = currentIndex < state.placedWordCount;
      
      // Increment placedWordCount BEFORE setting the word start, 
      // so that buildOverridesFromResolvedWords includes it!
      if (!wasAlreadyPlaced) {
        state.placedWordCount = Math.min(currentIndex + 1, state.resolvedWords.length);
      }

      if (!setWordStart(currentIndex, targetStart)) {
        if (!wasAlreadyPlaced) {
          state.placedWordCount = currentIndex;
        }
        return false;
      }
      
      if (!wasAlreadyPlaced) {
        const nextDraftIndex = Math.min(state.placedWordCount, Math.max(state.resolvedWords.length - 1, 0));
        state.selectedWordIndex = nextDraftIndex;
      } else {
        state.selectedWordIndex = currentIndex;
      }
      buildOverridesFromResolvedWords();
      return true;
    }

    function resetSelectedSuffix() {
      if (!state.baseWords.length) {
        return;
      }
      const keepWords = {};
      Object.entries(state.overrides.words || {}).forEach(([wordId, override]) => {
        const index = state.baseWords.findIndex((word) => word.id === wordId);
        if (index !== -1 && index < state.selectedWordIndex) {
          keepWords[wordId] = override;
        }
      });
      state.overrides.words = keepWords;
      state.placedWordCount = Math.min(state.selectedWordIndex, state.baseWords.length);
      state.overrides.placed_word_count = state.placedWordCount;
      syncResolvedWords();
      flushDataLayer();
      renderAll();
    }

    function resetAllOverrides() {
      state.overrides.words = {};
      state.overrides.lines = {};
      state.placedWordCount = 0;
      state.overrides.placed_word_count = 0;
      syncResolvedWords();
      renderAll();
    }

    function timeToPercent(time) {
      const span = Math.max(state.displayWindow.end - state.displayWindow.start, 0.001);
      return ((time - state.displayWindow.start) / span) * 100;
    }

    function isWordPlaced(index) {
      return index < state.placedWordCount;
    }

    function getDisplayStartForWord(index) {
      const word = state.resolvedWords[index];
      if (!word) {
        return null;
      }
      if (!isWordPlaced(index) && index === state.selectedWordIndex) {
        return clamp(ui.audio.currentTime || 0, state.displayWindow.start, state.displayWindow.end);
      }
      if (!Number.isFinite(word.start)) {
        return null;
      }
      return word.start;
    }

    function getDisplayEndForWord(index, startTime) {
      const word = state.resolvedWords[index];
      if (!word) {
        return null;
      }
      const isPlaced = isWordPlaced(index);
      const isDragging = state.wordDrag && state.wordDrag.wordIndex === index;
      if (!isPlaced && index !== state.selectedWordIndex) {
        return null;
      }
      if (!Number.isFinite(startTime)) {
        return null;
      }
      if (isPlaced && !isDragging && Number.isFinite(word.end)) {
        return word.end;
      }
      const vocalEnd = startTime + Math.max(word.duration || 0.12, 0.12);
      const vocalEndCapped = startTime + Math.min(word.duration || 0.12, 2.0);
      const nextWord = state.resolvedWords[index + 1];
      const isLastWord = index === state.resolvedWords.length - 1;
      const isNextPlaced = nextWord && isWordPlaced(index + 1);
      if (isLastWord) {
        return vocalEndCapped + 2.0;
      }
      if (isNextPlaced) {
        const nextStart = getDisplayStartForWord(index + 1);
        if (!Number.isFinite(nextStart)) {
          return vocalEndCapped + 2.0;
        }
        const gap = nextStart - vocalEndCapped;
        if (word.isLineEnd || gap > 2.0) {
          return Math.min(vocalEndCapped + 2.0, nextStart);
        }
        return nextStart;
      }
      if (word.isLineEnd) {
        return vocalEndCapped + 2.0;
      }
      return vocalEnd;
    }

    function getPreviewWordSnapshot() {
      const snapshot = new Map();

      state.resolvedWords.forEach((word, index) => {
        if (!isWordPlaced(index) || !Number.isFinite(word.start) || !Number.isFinite(word.end)) {
          return;
        }
        snapshot.set(word.id, {
          ...word,
          previewStart: word.start,
          previewEnd: word.end,
        });
      });

      return snapshot;
    }

    function getActivePreviewWord(previewWords) {
      const currentTime = ui.audio.currentTime || 0;
      const timedCandidates = [...previewWords.values()]
        .sort((a, b) => b.previewStart - a.previewStart);
      const playingWord = timedCandidates.find(
        (word) => currentTime >= word.previewStart && currentTime < word.previewEnd,
      );
      if (playingWord) {
        return playingWord;
      }

      return null;
    }

    function getVisibleWords() {
      const visible = [];
      for (let index = 0; index < state.resolvedWords.length; index += 1) {
        const isSelectedDraft = index === state.selectedWordIndex && !isWordPlaced(index);
        if (!isWordPlaced(index) && !isSelectedDraft) {
          continue;
        }
        const word = state.resolvedWords[index];
        const rawStart = getDisplayStartForWord(index);
        if (!Number.isFinite(rawStart)) {
          continue;
        }
        const displayEnd = getDisplayEndForWord(index, rawStart);
        visible.push({
          ...word,
          displayStart: rawStart,
          displayEnd: Number.isFinite(displayEnd) ? displayEnd : rawStart + Math.max(word.duration || 0.12, 0.12),
          isPlaced: isWordPlaced(index),
          isActive: index === state.selectedWordIndex,
        });
      }
      return visible.filter((word) => word.displayEnd >= state.displayWindow.start - 0.3 && word.displayStart <= state.displayWindow.end + 0.3);
    }

    function findPreviewLines() {
      const lines = getResolvedLines();
      if (!lines.length) return { activeIndex: -1, upcomingIndex: -1 };

      const currentTime = ui.audio.currentTime || 0;
      const previewWords = getPreviewWordSnapshot();
      
      let activeIndex = -1;
      let upcomingIndex = -1;

      // Find the active line
      for (const line of lines) {
        if (!line.word_ids || !line.word_ids.length) continue;
        const firstWord = previewWords.get(line.word_ids[0]);
        const lastWord = previewWords.get(line.word_ids[line.word_ids.length - 1]);
        if (!firstWord || !lastWord) continue;
        
        // A line is "active" (centered) roughly 0.2s before its first word, until its last word ends.
        if (currentTime >= firstWord.previewStart - 0.2 && currentTime <= lastWord.previewEnd) {
          activeIndex = line.index;
          break;
        }
      }

      if (activeIndex === -1) {
        // Gap state: find the NEXT line to be promoted
        for (const line of lines) {
          if (!line.word_ids || !line.word_ids.length) continue;
          const firstWord = previewWords.get(line.word_ids[0]);
          if (firstWord && currentTime < firstWord.previewStart - 0.2) {
            upcomingIndex = line.index;
            break;
          }
        }
      } else {
        upcomingIndex = activeIndex + 1;
      }

      return { activeIndex, upcomingIndex };
    }

    let previewScrollDOM = null;

    function initPreviewScroll() {
      const lines = getResolvedLines();
      if (!lines.length) return;
      
      ui.preview.innerHTML = "";
      previewScrollDOM = document.createElement("div");
      previewScrollDOM.className = "preview-scroll";
      
      lines.forEach((line) => {
        const row = document.createElement("div");
        row.className = "preview-line standby";
        row.dataset.lineIndex = line.index;
        
        const lineWords = Array.isArray(line.allWords) && line.allWords.length ? line.allWords : line.words;
        row.dir = inferDirection(line.text || lineWords.map((word) => word.text).join(" "));
        
        lineWords.forEach((word) => {
          const span = document.createElement("span");
          span.className = "preview-word hidden";
          span.textContent = word.text;
          span.dataset.wordId = word.id;
          row.appendChild(span);
        });
        
        previewScrollDOM.appendChild(row);
      });
      
      ui.preview.appendChild(previewScrollDOM);
    }

    function renderPreview() {
      const previewWords = getPreviewWordSnapshot();
      const activePreviewWord = getActivePreviewWord(previewWords);
      const { activeIndex, upcomingIndex } = findPreviewLines();

      if (!state.resolvedWords.length) {
        ui.preview.className = "preview empty";
        ui.preview.textContent = state.session?.project_id 
          ? "Project loaded. Paste lyrics in the text area to begin timing." 
          : "Import a project to begin.";
        previewScrollDOM = null;
        renderSelectedWordLabel(getSelectedWord());
        return;
      }

      ui.preview.className = "preview";
      
      if (!previewScrollDOM || previewScrollDOM.children.length !== getResolvedLines().length) {
        initPreviewScroll();
      }
      
      renderSelectedWordLabel(getSelectedWord());
      
      let targetOffset = 0;
      
      Array.from(previewScrollDOM.children).forEach((row) => {
        const lineIndex = Number(row.dataset.lineIndex);
        
        if (activeIndex !== -1) {
          // Standard active block
          if (lineIndex < activeIndex) {
            row.className = "preview-line past";
          } else if (lineIndex === activeIndex) {
            row.className = "preview-line current";
            targetOffset = -row.offsetTop - (row.offsetHeight / 2);
          } else if (lineIndex === upcomingIndex) {
            row.className = "preview-line upcoming";
          } else {
            row.className = "preview-line standby";
          }
        } else {
          // Gap state
          if (upcomingIndex !== -1) {
            if (lineIndex < upcomingIndex) {
               row.className = "preview-line past";
            } else if (lineIndex === upcomingIndex) {
               row.className = "preview-line upcoming";
               targetOffset = -row.offsetTop - (row.offsetHeight / 2) + 50; 
            } else {
               row.className = "preview-line standby";
            }
          } else {
            row.className = "preview-line past";
            targetOffset = -previewScrollDOM.offsetHeight;
          }
        }
        
        // Update words inside row
        Array.from(row.children).forEach((span) => {
          const wordId = span.dataset.wordId;
          const word = state.resolvedWords.find(w => w.id === wordId);
          if (!word || !isWordPlaced(word.index)) {
            span.className = "preview-word hidden";
          } else if (activePreviewWord && word.id === activePreviewWord.id) {
            span.className = "preview-word current done";
          } else {
            span.className = "preview-word done";
          }
        });
      });
      
      previewScrollDOM.style.transform = `translateY(${targetOffset}px)`;
    }

    function renderWordLayer() {
      ui.wordLayer.innerHTML = "";
      const visibleWords = getVisibleWords();

      visibleWords.forEach((word) => {
        const element = document.createElement("button");
        element.type = "button";
        element.className = "word";
        if (word.index % 2 === 1) {
          element.classList.add("alt");
        }
        if (word.isActive) {
          element.classList.add("active");
          if (!word.isPlaced) {
            element.classList.add("draft");
          }
        }
        if (word.isPlaced) {
          element.classList.add("done");
          element.classList.add("draggable");
        }
        if (state.wordDrag && state.wordDrag.wordIndex === word.index) {
          element.classList.add("dragging");
        }
        const leftPercent = timeToPercent(word.displayStart);
        element.style.left = `${leftPercent}%`;
        element.style.width = "max-content"; // Ensure button shrinks exactly to text
        
        // Strip out invisible formatting characters that might bloat the width
        const cleanText = String(word.text || "").replace(/[\s\u200B-\u200D\uFEFF\u00A0]+/g, '');
        element.textContent = cleanText;
        element.dataset.wordId = word.id;
        element.dir = inferDirection(cleanText);
        if (word.isPlaced) {
          element.addEventListener("pointerdown", (event) => {
            beginWordDrag(event, word.index);
          });
        }
        element.addEventListener("click", (event) => {
          event.preventDefault();
          if (state._suppressClick) return;
          setSelectedWord(word.index);
        });
        ui.wordLayer.appendChild(element);
      });
    }

    function drawWaveform() {
      const canvas = ui.waveCanvas;
      const rect = canvas.getBoundingClientRect();
      const ratio = window.devicePixelRatio || 1;
      const width = Math.max(Math.floor(rect.width * ratio), 1);
      const height = Math.max(Math.floor(rect.height * ratio), 1);

      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }

      const context = canvas.getContext("2d");
      context.clearRect(0, 0, width, height);
      context.fillStyle = "#090c12";
      context.fillRect(0, 0, width, height);

      if (!state.waveform || !state.waveform.peaks || !state.waveform.peaks.length) {
        context.fillStyle = "rgba(255,255,255,0.35)";
        context.font = `${16 * ratio}px Aptos, Segoe UI, sans-serif`;
        context.fillText("Waveform unavailable", 24 * ratio, height / 2);
        return;
      }

      const peaks = state.waveform.peaks;
      const duration = Math.max(state.waveform.duration || 0, 0.001);
      const startTime = state.displayWindow.start;
      const endTime = state.displayWindow.end;
      const visibleStart = Math.max(startTime, 0);
      const visibleEnd = Math.min(endTime, duration);
      const mid = height / 2;

      context.strokeStyle = "rgba(143, 160, 182, 0.72)";
      context.lineWidth = Math.max(1, ratio);
      context.beginPath();

      if (visibleEnd > visibleStart) {
        const startIndex = clamp(Math.floor((visibleStart / duration) * peaks.length), 0, peaks.length - 1);
        const endIndex = clamp(Math.ceil((visibleEnd / duration) * peaks.length), startIndex + 1, peaks.length);
        for (let index = startIndex; index < endIndex; index += 1) {
          const amplitude = peaks[index];
          const sampleTime = (index / Math.max(peaks.length - 1, 1)) * duration;
          const x = (timeToPercent(sampleTime) / 100) * width;
          if (x < -2 || x > width + 2) {
            continue;
          }
          const bar = Math.max(amplitude * (height * 0.42), 1 * ratio);
          context.moveTo(x, mid - bar);
          context.lineTo(x, mid + bar);
        }
      }
      context.stroke();

      const selected = getSelectedWord();
      if (selected) {
        const displayStart = getDisplayStartForWord(state.selectedWordIndex);
        if (!Number.isFinite(displayStart)) {
          return;
        }
        const displayEnd = getDisplayEndForWord(state.selectedWordIndex, displayStart);
        if (!Number.isFinite(displayEnd)) {
          return;
        }
        if (displayEnd < state.displayWindow.start || displayStart > state.displayWindow.end) {
          return;
        }
        
        const startX = (timeToPercent(displayStart) / 100) * width;
        const endX = (timeToPercent(displayEnd) / 100) * width;

        context.fillStyle = "rgba(228, 178, 29, 0.12)";
        context.fillRect(startX, 0, Math.max(endX - startX, 2), height);
      }
    }

    function renderButtons() {
      const hasWords = state.resolvedWords.length > 0;
      const hasProject = Boolean(state.session?.project_id);
      [
        ui.zoomOut,
        ui.zoomIn,
        ui.zoomReset,
        ui.prevWord,
        ui.nextWord,
        ui.lastWord,
        ui.placeWord,
        ui.rewindStart,
        ui.playPause,
        ui.back2,
        ui.jumpWord,
        ui.exportMp4,
        ui.resetWord,
        ui.resetAll,
      ].forEach((button) => {
        button.disabled = !hasWords;
      });
      if (ui.saveProject) {
        ui.saveProject.disabled = !hasProject;
      }
      if (ui.deleteProject) {
        ui.deleteProject.disabled = !hasProject;
      }
      if (ui.aiFirstPass) {
        ui.aiFirstPass.disabled = !hasProject || Boolean(state.pipelineRunning);
      }
      if (ui.stopAiPass) {
        ui.stopAiPass.hidden = !Boolean(state.pipelineRunning);
      }
      if (ui.jumpWord) {
        const selected = getSelectedWord();
        ui.jumpWord.disabled = !hasWords || !selected || !Number.isFinite(selected.start);
      }
      if (ui.playPause) {
        ui.playPause.textContent = ui.audio.paused ? "▶" : "⏸";
      }
      if (ui.stemMix) {
        ui.stemMix.disabled = !hasWords || !state.stemMixAvailable;
      }
      if (ui.rewindStart) {
        ui.rewindStart.textContent = "⏮";
      }
      if (ui.zoomOut) {
        ui.zoomOut.textContent = "-";
      }
      if (ui.zoomIn) {
        ui.zoomIn.textContent = "+";
      }
      if (ui.zoomReset) {
        ui.zoomReset.textContent = "100";
      }
    }

    function renderAll() {
      updateWindow();
      ui.playhead.style.left = "50%";
      renderPreview();
      renderWordLayer();
      drawWaveform();
      renderButtons();
    }

    function queueRender() {
      if (state.redrawQueued) {
        return;
      }
      state.redrawQueued = true;
      requestAnimationFrame(() => {
        state.redrawQueued = false;
        renderAll();
      });
    }

    function beginPlayheadDrag(event) {
      event.preventDefault();
      if (!hasProject()) {
        return;
      }

      const waveRect = ui.waveCanvas.getBoundingClientRect();
      state.playheadDrag = {
        pointerId: event.pointerId,
        width: Math.max(waveRect.width, 1),
        originX: event.clientX,
        originTime: ui.audio.currentTime || 0,
      };
      window.addEventListener("pointermove", onPlayheadDragMove);
      window.addEventListener("pointerup", endPlayheadDrag);
      window.addEventListener("pointercancel", endPlayheadDrag);
    }

    function onPlayheadDragMove(event) {
      if (!state.playheadDrag || event.pointerId !== state.playheadDrag.pointerId) {
        return;
      }
      const deltaX = event.clientX - state.playheadDrag.originX;
      const deltaSeconds = (deltaX / state.playheadDrag.width) * state.zoomSeconds * PLAYHEAD_DRAG_SENSITIVITY;
      setPlayheadTime(state.playheadDrag.originTime + deltaSeconds);
      queueRender();
    }

    function endPlayheadDrag(event) {
      if (!state.playheadDrag || (event.pointerId !== undefined && event.pointerId !== state.playheadDrag.pointerId)) {
        return;
      }
      state.playheadDrag = null;
      window.removeEventListener("pointermove", onPlayheadDragMove);
      window.removeEventListener("pointerup", endPlayheadDrag);
      window.removeEventListener("pointercancel", endPlayheadDrag);
      renderAll();
    }

    function beginWordDrag(event, wordIndex) {
      if (!hasProject() || !isWordPlaced(wordIndex)) {
        return;
      }

      event.preventDefault();
      event.stopPropagation();
      const draggedWord = state.resolvedWords[wordIndex];
      if (!draggedWord || !Number.isFinite(draggedWord.start)) {
        return;
      }

      const waveRect = ui.waveCanvas.getBoundingClientRect();
      const displayStart = getDisplayStartForWord(wordIndex);
      state.wordDrag = {
        pointerId: event.pointerId,
        width: Math.max(waveRect.width, 1),
        originX: event.clientX,
        originStart: draggedWord.start,
        wordIndex,
        anchorPercent: clamp(timeToPercent(displayStart), 0, 100),
        originalStarts: state.resolvedWords.map(w => w.start),
      };
      state.selectedWordIndex = wordIndex;
      state._suppressClick = true;
      if (event.currentTarget && typeof event.currentTarget.setPointerCapture === "function") {
        event.currentTarget.setPointerCapture(event.pointerId);
      }
      window.addEventListener("pointermove", onWordDragMove);
      window.addEventListener("pointerup", endWordDrag);
      window.addEventListener("pointercancel", endWordDrag);
      queueRender();
    }

    function onWordDragMove(event) {
      if (!state.wordDrag || event.pointerId !== state.wordDrag.pointerId) {
        return;
      }
      const deltaX = event.clientX - state.wordDrag.originX;
      const deltaSeconds = (deltaX / state.wordDrag.width) * state.zoomSeconds;
      setDraggedWordStart(state.wordDrag.wordIndex, state.wordDrag.originStart + deltaSeconds);
      queueRender();
    }

    function endWordDrag(event) {
      if (!state.wordDrag || (event.pointerId !== undefined && event.pointerId !== state.wordDrag.pointerId)) {
        return;
      }
      const draggedIndex = state.wordDrag.wordIndex;
      state.wordDrag = null;
      state._suppressClick = false;
      window.removeEventListener("pointermove", onWordDragMove);
      window.removeEventListener("pointerup", endWordDrag);
      window.removeEventListener("pointercancel", endWordDrag);
      flushDataLayer();
      renderAll();
      state.selectedWordIndex = draggedIndex;
      renderAll();
    }

    async function fetchJson(url, options) {
      const response = await fetch(url, options);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = payload && payload.error ? payload.error : `Request failed: ${response.status}`;
        throw new Error(error);
      }
      return payload;
    }

    async function loadProject(options = {}) {
      ui.audio.pause();
      pauseStemPlayers();
      try {
        ui.audio.currentTime = 0;
      } catch (_) {
        // ignore media reset errors
      }
      state.manifest = { lines: [], words: [] };
      state.overrides = { version: 1, global_offset: 0, placed_word_count: 0, lines: {}, words: {} };
      state.waveform = null;
      state.baseWords = [];
      state.resolvedWords = [];
      state.placedWordCount = 0;
      state.selectedWordIndex = 0;
      state.zoomSeconds = DEFAULT_ZOOM_SECONDS;
      state.displayWindow = { start: 0, end: DEFAULT_ZOOM_SECONDS };
      state.wordDrag = null;
      ui.lyricsText.value = "";
      updateLyricsSourceLink("");
      renderAll();

      const bust = options.bustCache ? `?t=${Date.now()}` : "";
      const projectsPayload = await fetchJson(`/api/projects${bust}`);
      state.projects = Array.isArray(projectsPayload.projects) ? projectsPayload.projects : [];
      renderProjectSelect(projectsPayload.current_project_id);
      state.session = await fetchJson(`/api/session${bust}`);
      if (ui.projectName && typeof state.session.project_name === "string") {
        ui.projectName.value = state.session.project_name;
      }
      ui.pathsStatus.textContent = displayOutputName(state.session.output_video_name);
      if (typeof state.session.lyrics_text === "string") {
        ui.lyricsText.value = state.session.lyrics_text;
      }
      updateLyricsSourceLink(state.session.lyrics_source_url);
      renderProjectSelect(state.session.project_id);

      try {
        state.manifest = await fetchJson(`/api/manifest${bust}`);
      } catch (_) {
        state.manifest = { lines: [], words: [] };
      }
      try {
        state.overrides = await fetchJson(`/api/overrides${bust}`);
      } catch (_) {
        state.overrides = { version: 1, global_offset: 0, placed_word_count: 0, lines: {}, words: {} };
      }
      try {
        state.waveform = await fetchJson(`/api/waveform?bins=2600&t=${Date.now()}`);
        setupStemPlaybackSources();
      } catch (_) {
        state.waveform = null;
        ui.audio.pause();
        pauseStemPlayers();
        try {
          ui.audio.currentTime = 0;
        } catch (_) {
          // ignore media reset errors
        }
        ui.audio.removeAttribute("src");
        ui.audio.load();
        ui.vocalsAudio.removeAttribute("src");
        ui.vocalsAudio.load();
        ui.musicAudio.removeAttribute("src");
        ui.musicAudio.load();
        state.stemMixAvailable = false;
        applyStemMix();
      }

      if (!ui.lyricsText.value.trim()) {
        ui.lyricsText.value = deriveLyricsTextFromManifest();
      }

      syncResolvedWords();
      if (state.waveform) {
        updateStatusChip(ui.projectStatus, `Ready: ${state.resolvedWords.length} words`, state.resolvedWords.length ? "ok" : undefined);
      } else if (state.session && state.session.status === "empty" && !state.session.has_audio) {
        updateStatusChip(ui.projectStatus, "Project saved without audio. Choose MP3 or paste a YouTube URL, then click Create Project.", "warn");
      } else if (state.session.project_name) {
        updateStatusChip(ui.projectStatus, `Project ready: ${state.session.project_name}`, "ok");
      }
      try {
        const exportStatus = await syncExportStatus();
        if (["queued", "building_subtitles", "rendering"].includes(exportStatus.status)) {
          ui.exportMp4.disabled = true;
          startExportPolling();
        } else {
          ui.exportMp4.disabled = false;
        }
      } catch (_) {
        renderExportStatus({ status: "idle", detail: "Idle", progress: 0 });
      }
      try {
        const pipelineStatus = await syncPipelineStatus();
        if (pipelineStatus.status === "running") {
          startPipelinePolling();
        } else {
          stopPipelinePolling();
        }
      } catch (_) {
        renderPipelineStatus({ status: "idle", stages: [] });
      }
      renderAll();
    }

    async function importProject() {
      const projectName = ui.projectName.value.trim();
      const lyricsText = ui.lyricsText.value.trim();
      const hasAudio = Boolean(ui.audioFile.files && ui.audioFile.files[0]);

      if (!hasAudio) {
        ui.importProject.disabled = true;
        updateStatusChip(ui.projectStatus, "Creating project...");
        try {
          await fetchJson("/api/projects/create", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              project_name: projectName,
              lyrics_text: lyricsText,
            }),
          });
          await loadProject({ bustCache: true });
        } catch (error) {
          updateStatusChip(ui.projectStatus, error.message, "warn");
        } finally {
          ui.importProject.disabled = false;
        }
        return;
      }

      const form = new FormData();
      form.append("audio_file", ui.audioFile.files[0]);
      form.append("project_name", ui.audioFile.files[0].name.replace(/\.[^.]+$/, ""));

      ui.importProject.disabled = true;
      updateStatusChip(ui.projectStatus, "Searching lyrics and importing project...");

      try {
        const responsePayload = await fetchJson("/api/import/audio-auto", { method: "POST", body: form });
        
        // Clear the file input immediately after successful upload
        ui.audioFile.value = "";
        
        // Load project now (audio.wav + lyrics are ready synchronously)
        await loadProject({ bustCache: true });
        
        const hasLyrics = Boolean(String(state.session?.lyrics_text || "").trim());
        if (responsePayload.lyrics_found && responsePayload.lyrics_source_url && hasLyrics) {
          updateLyricsSourceLink(responsePayload.lyrics_source_url);
          updateStatusChip(ui.projectStatus, responsePayload.lyrics_title ? `Lyrics imported: ${responsePayload.lyrics_title}` : "Lyrics imported.", "ok");
        } else {
          updateLyricsSourceLink("");
          updateStatusChip(ui.projectStatus, "No lyrics found. Stems processing...", "warn");
        }

        // Stems are still processing in background — poll until complete
        if (responsePayload.status === "running") {
          state.pipelineAwaitingProjectLoad = true;
          try {
            const pipelineStatus = await syncPipelineStatus();
            if (pipelineStatus.status === "running") {
              startPipelinePolling();
            } else if (pipelineStatus.status === "completed") {
              await stopPipelineAndLoadProject();
              updateStatusChip(ui.projectStatus, "Stems ready.", "ok");
            }
          } catch (_) {
            startPipelinePolling();
          }
        }
      } catch (error) {
        updateStatusChip(ui.projectStatus, error.message, "warn");
      } finally {
        ui.importProject.disabled = false;
      }
    }

    async function createProjectFromPanel() {
      if (ui.youtubeUrl.value.trim()) {
        await importYoutubeAudio();
        return;
      }
      await importProject();
    }

    async function importYoutubeAudio() {
      const youtubeUrl = ui.youtubeUrl.value.trim();
      if (!youtubeUrl) {
        updateStatusChip(ui.projectStatus, "Paste a YouTube URL first.", "warn");
        return;
      }

      const currentProjectId = state.session?.project_id || ui.projectSelect?.value || "";
      const shouldAttachToExisting =
        Boolean(currentProjectId)
        && state.session
        && state.session.status === "empty"
        && !state.session.has_audio;

      const payload = {
        youtube_url: youtubeUrl,
      };
      if (shouldAttachToExisting) {
        payload.project_id = currentProjectId;
      }

      ui.importProject.disabled = true;
      updateStatusChip(ui.projectStatus, "Downloading audio from YouTube...");

      try {
        const responsePayload = await fetchJson("/api/import/youtube", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        ui.youtubeUrl.value = "";
        
        // Load project immediately (audio.wav + lyrics are ready synchronously)
        await loadProject({ bustCache: true });
        
        const hasLyrics = Boolean(String(state.session?.lyrics_text || "").trim());
        if (responsePayload.lyrics_source_url && hasLyrics) {
          updateLyricsSourceLink(responsePayload.lyrics_source_url);
          updateStatusChip(ui.projectStatus, responsePayload.lyrics_title ? `Lyrics imported: ${responsePayload.lyrics_title}` : "Lyrics imported. Stems processing...", "ok");
        } else {
          updateLyricsSourceLink("");
          updateStatusChip(ui.projectStatus, "No lyrics found. Stems processing...", "warn");
        }

        // Stems are still processing in background — poll until complete
        if (responsePayload.status === "running") {
          state.pipelineAwaitingProjectLoad = true;
          try {
            const pipelineStatus = await syncPipelineStatus();
            if (pipelineStatus.status === "running") {
              startPipelinePolling();
            } else if (pipelineStatus.status === "completed") {
              await stopPipelineAndLoadProject();
              updateStatusChip(ui.projectStatus, "Stems ready.", "ok");
            }
          } catch (_) {
            startPipelinePolling();
          }
          return;
        }
        stopPipelinePolling();
        renderPipelineStatus(responsePayload);
        await loadProject({ bustCache: true });
        if (responsePayload.lyrics_source_url && responsePayload.lyrics_found !== false && String(state.session?.lyrics_text || "").trim()) {
          updateLyricsSourceLink(responsePayload.lyrics_source_url);
        } else {
          updateLyricsSourceLink("");
        }
        updateStatusChip(
          ui.projectStatus,
          responsePayload.lyrics_found === false || !String(state.session?.lyrics_text || "").trim()
            ? "No lyrics found."
            : (responsePayload.lyrics_title ? `Lyrics imported: ${responsePayload.lyrics_title}` : "Audio imported and project built."),
          responsePayload.lyrics_found === false || !String(state.session?.lyrics_text || "").trim() ? "warn" : "ok",
        );
      } catch (error) {
        updateStatusChip(ui.projectStatus, error.message, "warn");
      } finally {
        ui.importProject.disabled = false;
      }
    }

    async function flushDataLayer() {
      try {
        buildOverridesFromResolvedWords();
        const payload = {
          project_id: state.session?.project_id || ui.projectSelect?.value || "",
          project_name: state.session?.project_name || "",
          lyrics_text: ui.lyricsText.value.trim(),
          version: 1,
          global_offset: 0,
          placed_word_count: state.placedWordCount,
          lines: {},
          words: state.overrides.words,
        };
        await fetchJson("/api/overrides", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
      } catch (e) {
        console.error("Data flush failed:", e);
      }
    }

    async function saveProject() {
      try {
        buildOverridesFromResolvedWords();
        const originalProjectName = String(state.session?.project_name || "").trim();
        const requestedProjectName = ui.projectName.value.trim();
        const renameRequested = Boolean(requestedProjectName && requestedProjectName !== originalProjectName);
        const payload = {
          project_id: state.session?.project_id || ui.projectSelect?.value || "",
          project_name: requestedProjectName,
          lyrics_text: ui.lyricsText.value.trim(),
          version: 1,
          global_offset: 0,
          placed_word_count: state.placedWordCount,
          lines: {},
          words: state.overrides.words,
        };
        let responsePayload;
        let fallbackUsed = false;
        try {
          responsePayload = await fetchJson("/api/projects/save", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
        } catch (error) {
          if (!String(error.message || "").includes("404")) {
            throw error;
          }
          fallbackUsed = true;
          responsePayload = await fetchJson("/api/overrides", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
        }
        await loadProject({ bustCache: true });
        updateStatusChip(
          ui.projectStatus,
          fallbackUsed
            ? (
              renameRequested
                ? "Saved timing overrides only. Restart the timing editor server, then save again to commit the rename."
                : "Saved timing overrides. Restart the timing editor server to enable project-save route updates."
            )
            : `Saved project: ${responsePayload.project_name}`,
          "ok",
        );
      } catch (error) {
        updateStatusChip(ui.projectStatus, error.message, "warn");
      }
    }

    async function deleteProject() {
      const projectId = state.session?.project_id || ui.projectSelect?.value || "";
      if (!projectId) {
        updateStatusChip(ui.projectStatus, "No project is loaded.", "warn");
        return;
      }
      if (!window.confirm(`Delete project "${ui.projectName.value.trim() || projectId}"?`)) {
        return;
      }

      try {
        await fetchJson("/api/projects/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project_id: projectId }),
        });
        await loadProject({ bustCache: true });
        updateStatusChip(ui.projectStatus, "Project deleted.", "ok");
      } catch (error) {
        if (String(error.message || "").includes("404")) {
          updateStatusChip(
            ui.projectStatus,
            "Delete route is not available in the running server. Restart the timing editor, then try Delete Project again.",
            "warn",
          );
          return;
        }
        updateStatusChip(ui.projectStatus, error.message, "warn");
      }
    }

    async function exportMp4() {
      try {
        ui.exportMp4.disabled = true;
        updateStatusChip(ui.projectStatus, "Rendering MP4...");
        buildOverridesFromResolvedWords();
        await fetchJson("/api/overrides", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            version: 1,
            global_offset: 0,
            placed_word_count: state.placedWordCount,
            lyrics_text: ui.lyricsText.value.trim(),
            lines: {},
            words: state.overrides.words,
          }),
        });
        state.exportLocalStartMs = Date.now();
        let payload = await fetchJson("/api/export/mp4", { method: "POST" });
        renderExportStatus(payload);
        startExportPolling();
        while (["queued", "building_subtitles", "rendering"].includes(payload.status)) {
          const detail = payload.detail ? `${payload.detail}` : payload.status;
          const percent = `${estimatedExportProgress(payload)}%`;
          updateStatusChip(ui.projectStatus, `Rendering MP4: ${detail} ${percent}`.trim());
          await new Promise((resolve) => setTimeout(resolve, 2000));
          payload = await syncExportStatus();
        }
        stopExportPolling();
        if (payload.status === "error") {
          throw new Error(payload.error || "MP4 export failed");
        }
        updateStatusChip(ui.projectStatus, `Exported ${payload.output_video}`, "ok");
        ui.pathsStatus.textContent = displayOutputName(payload.output_video || state.session?.output_video_name);
        renderExportStatus(payload);
      } catch (error) {
        stopExportPolling();
        updateStatusChip(ui.projectStatus, error.message, "warn");
      } finally {
        ui.exportMp4.disabled = false;
      }
    }

    async function selectProject() {
      const projectId = ui.projectSelect?.value || "";
      if (!projectId) {
        return;
      }
      await fetchJson("/api/projects/select", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId }),
      });
      await loadProject({ bustCache: true });
    }

    function placeSelectedWordAtPlayhead() {
      const selected = getSelectedWord();
      if (!selected) {
        return;
      }
      let targetStart = ui.audio.currentTime || 0;

      // Fix for Mounted Words: Real-time chaining when placing multiple words while paused
      if (ui.audio.paused && state.selectedWordIndex > 0) {
        const previousWord = state.resolvedWords[state.selectedWordIndex - 1];
        if (previousWord && Number.isFinite(previousWord.end)) {
          // If the playhead hasn't moved past the previous word's start, automatically chain it
          if (targetStart <= previousWord.start + 0.05) {
            targetStart = previousWord.end;
            setPlayheadTime(targetStart);
          }
        }
      }

      const changed = commitCurrentWord(targetStart);
      if (changed) {
        flushDataLayer();
        renderAll();
      }
    }

    function seekRelative(delta) {
      ui.audio.currentTime = clamp((ui.audio.currentTime || 0) + delta, 0, getAudioDuration());
      renderAll();
    }

    function seekToStart() {
      ui.audio.currentTime = 0;
      setWindowAround(0);
      renderAll();
    }

    function jumpToSelectedWord() {
      const selected = getSelectedWord();
      if (!selected || !Number.isFinite(selected.start)) {
        return;
      }
      ui.audio.currentTime = clamp(selected.start, 0, getAudioDuration());
      renderAll();
    }

    function jumpToLastPendingWord() {
      if (!state.resolvedWords.length) {
        return;
      }
      const targetIndex = state.placedWordCount < state.resolvedWords.length
        ? state.placedWordCount
        : Math.max(state.resolvedWords.length - 1, 0);
      setSelectedWord(targetIndex, { seek: isWordPlaced(targetIndex) });
    }

    function resetZoomLevel() {
      state.zoomSeconds = clamp(
        DEFAULT_ZOOM_SECONDS,
        MIN_ZOOM_SECONDS,
        Math.max(getAudioDuration() || DEFAULT_ZOOM_SECONDS, MIN_ZOOM_SECONDS),
      );
      setWindowAround(ui.audio.currentTime || 0);
      renderAll();
    }

    function seekFromWaveform(event) {
      if (!hasProject()) {
        return;
      }
      const rect = ui.waveCanvas.getBoundingClientRect();
      const ratio = clamp((event.clientX - rect.left) / rect.width, 0, 1);
      const target = state.displayWindow.start + ratio * (state.displayWindow.end - state.displayWindow.start);
      setPlayheadTime(target);
      renderAll();
    }

    function panWaveformWindow(targetStart) {
      setPlayheadTime(targetStart);
      renderAll();
    }

    function isTypingTarget(target) {
      if (!target) {
        return false;
      }
      const element = target instanceof Element ? target : document.activeElement;
      if (!(element instanceof Element)) {
        return false;
      }
      if (element.closest("textarea")) {
        return true;
      }
      const input = element.closest("input");
      if (input) {
        return true;
      }
      if (element.closest("select")) {
        return true;
      }
      return element.isContentEditable;
    }

    function handleEditorShortcut(event) {
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) {
        return;
      }
      if (isTypingTarget(document.activeElement)) {
        return;
      }

      const key = String(event.key || "").toLowerCase();
      switch (event.code) {
        case "Space":
          if (!hasProject()) {
            return;
          }
          event.preventDefault();
          if (ui.audio.paused) {
            ui.audio.play().catch(() => {});
          } else {
            ui.audio.pause();
          }
          return;
        case "Enter":
          if (!hasProject()) {
            return;
          }
          event.preventDefault();
          placeSelectedWordAtPlayhead();
          return;
        case "ArrowRight":
          if (!state.resolvedWords.length) {
            return;
          }
          event.preventDefault();
          setSelectedWord(state.selectedWordIndex + 1, { seek: true });
          return;
        case "ArrowLeft":
          if (!state.resolvedWords.length) {
            return;
          }
          event.preventDefault();
          setSelectedWord(state.selectedWordIndex - 1, { seek: true });
          return;
        case "ArrowDown":
          if (!hasProject()) {
            return;
          }
          event.preventDefault();
          seekRelative(-2);
          return;
        case "Home":
          if (!hasProject()) {
            return;
          }
          event.preventDefault();
          seekToStart();
          return;
        default:
          break;
      }

      if (key === "j") {
        if (!state.resolvedWords.length) {
          return;
        }
        event.preventDefault();
        jumpToSelectedWord();
        return;
      }
      if (key === "r") {
        if (!state.resolvedWords.length) {
          return;
        }
        event.preventDefault();
        resetSelectedSuffix();
        return;
      }
      if (key === "l") {
        if (!state.resolvedWords.length) {
          return;
        }
        event.preventDefault();
        jumpToLastPendingWord();
        return;
      }
      if (key === "0") {
        if (!hasProject()) {
          return;
        }
        event.preventDefault();
        resetZoomLevel();
      }
    }

    function bindEvents() {
      ui.importProject.addEventListener("click", () => createProjectFromPanel().catch((error) => {
        updateStatusChip(ui.projectStatus, error.message, "warn");
      }));
      if (ui.projectSelect) {
        ui.projectSelect.addEventListener("change", () => selectProject().catch((error) => {
          updateStatusChip(ui.projectStatus, error.message, "warn");
        }));
      }
      if (ui.youtubeUrl) {
        ui.youtubeUrl.addEventListener("keydown", (event) => {
          if (event.key !== "Enter") {
            return;
          }
          event.preventDefault();
          createProjectFromPanel().catch((error) => {
            updateStatusChip(ui.projectStatus, error.message, "warn");
          });
        });
      }
      if (ui.aiFirstPass) {
        ui.aiFirstPass.addEventListener("click", () => runAiFirstPass().catch((error) => {
          updateStatusChip(ui.projectStatus, error.message, "warn");
        }));
      }
      if (ui.stopAiPass) {
        ui.stopAiPass.addEventListener("click", () => stopAiFirstPass().catch((error) => {
          updateStatusChip(ui.projectStatus, error.message, "warn");
        }));
      }
      ui.lyricsText.addEventListener("input", scheduleLyricsTextRebuild);

      ui.zoomOut.addEventListener("click", () => {
        state.zoomSeconds = clamp(
          state.zoomSeconds * 1.35,
          MIN_ZOOM_SECONDS,
          Math.max(getAudioDuration() || DEFAULT_ZOOM_SECONDS, MIN_ZOOM_SECONDS),
        );
        setWindowAround(ui.audio.currentTime || 0);
        renderAll();
      });

      ui.zoomIn.addEventListener("click", () => {
        state.zoomSeconds = clamp(
          state.zoomSeconds / 1.35,
          MIN_ZOOM_SECONDS,
          Math.max(getAudioDuration() || DEFAULT_ZOOM_SECONDS, MIN_ZOOM_SECONDS),
        );
        setWindowAround(ui.audio.currentTime || 0);
        renderAll();
      });
      if (ui.zoomReset) {
        ui.zoomReset.addEventListener("click", resetZoomLevel);
      }

      ui.prevWord.addEventListener("click", () => setSelectedWord(state.selectedWordIndex - 1, { seek: true }));
      ui.nextWord.addEventListener("click", () => setSelectedWord(state.selectedWordIndex + 1, { seek: true }));
      if (ui.lastWord) {
        ui.lastWord.addEventListener("click", jumpToLastPendingWord);
      }
      ui.placeWord.addEventListener("click", placeSelectedWordAtPlayhead);
      ui.playPause.addEventListener("click", () => {
        if (ui.audio.paused) {
          ui.audio.play().catch(() => {});
        } else {
          ui.audio.pause();
        }
      });
      if (ui.stemMix) {
        ui.stemMix.addEventListener("input", (event) => {
          state.stemMixValue = clampUnit(Number(event.target.value || 0));
          applyStemMix();
        });
      }
      ui.rewindStart.addEventListener("click", seekToStart);
      ui.back2.addEventListener("click", () => seekRelative(-2));
      ui.jumpWord.addEventListener("click", jumpToSelectedWord);
      ui.exportMp4.addEventListener("click", exportMp4);
      ui.saveProject.addEventListener("click", saveProject);
      ui.deleteProject.addEventListener("click", deleteProject);
      ui.resetWord.addEventListener("click", resetSelectedSuffix);
      ui.resetAll.addEventListener("click", resetAllOverrides);
      ui.waveCanvas.addEventListener("click", seekFromWaveform);
      ui.wavePan.addEventListener("input", (event) => {
        panWaveformWindow(Number(event.target.value || 0));
      });
      ui.playhead.addEventListener("pointerdown", beginPlayheadDrag);
      document.addEventListener("keydown", handleEditorShortcut);

      ["timeupdate", "seeked", "loadedmetadata", "durationchange", "play", "pause"].forEach((eventName) => {
        ui.audio.addEventListener(eventName, queueRender);
      });
      ["seeked", "play"].forEach((eventName) => {
        ui.audio.addEventListener(eventName, () => syncStemPlayback({ force: true }));
      });
      ui.audio.addEventListener("pause", pauseStemPlayers);
      ui.audio.addEventListener("ratechange", () => {
        ui.vocalsAudio.playbackRate = ui.audio.playbackRate || 1;
        ui.musicAudio.playbackRate = ui.audio.playbackRate || 1;
      });
      ui.vocalsAudio.addEventListener("loadedmetadata", refreshStemAvailability);
      ui.musicAudio.addEventListener("loadedmetadata", refreshStemAvailability);
      ui.vocalsAudio.addEventListener("error", refreshStemAvailability);
      ui.musicAudio.addEventListener("error", refreshStemAvailability);

      window.addEventListener("resize", queueRender);
    }

    bindEvents();
    state.stemMixValue = 0;
    if (ui.stemMix) {
      ui.stemMix.value = "0";
    }
    applyStemMix();
    renderAll();
    loadProject({ bustCache: true }).catch((error) => {
      updateStatusChip(ui.projectStatus, error.message, "warn");
      renderAll();
    });
  