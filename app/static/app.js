(function () {
  const form = document.querySelector("[data-new-form]");
  if (!form) {
    return;
  }
  const file = form.querySelector("input[type=file]");
  const url = form.querySelector("input[name=url]");
  const box = form.parentElement.querySelector("[data-error]") || form.querySelector("[data-error]");
  form.addEventListener("submit", function (event) {
    const hasFile = Boolean(file && file.files && file.files.length > 0);
    const hasUrl = Boolean(url && url.value.trim());
    if (hasFile === hasUrl) {
      event.preventDefault();
      const message = hasFile ? "文件和链接只能提交一种" : "请上传文件或粘贴链接";
      if (box) {
        box.hidden = false;
        box.textContent = message;
      }
    }
  });
})();

(function () {
  var button = document.querySelector("[data-theme-toggle]");
  if (!button) {
    return;
  }

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "studio" ? "studio" : "ledger";
  }

  function syncIcon(theme) {
    var sun = button.querySelector('[data-icon="sun"]');
    var moon = button.querySelector('[data-icon="moon"]');
    if (sun) {
      sun.hidden = theme !== "ledger";
    }
    if (moon) {
      moon.hidden = theme !== "studio";
    }
  }

  syncIcon(currentTheme());
  button.addEventListener("click", function () {
    var next = currentTheme() === "ledger" ? "studio" : "ledger";
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem("theme", next);
    } catch {
      /* 隐私模式或配额不足时仍保留当前页的 data-theme */
    }
    syncIcon(next);
  });
})();

(function () {
  var zone = document.querySelector("[data-dropzone]");
  var fileInput = zone ? zone.querySelector("input[type=file]") : null;
  var nameEl = zone ? zone.querySelector("[data-file-name]") : null;

  function showName() {
    if (!nameEl || !fileInput) {
      return;
    }
    var file = fileInput.files && fileInput.files[0];
    if (!file) {
      return;
    }
    nameEl.hidden = false;
    nameEl.textContent = file.name;
  }

  if (zone && fileInput) {
    var depth = 0;
    fileInput.addEventListener("change", showName);
    zone.addEventListener("dragenter", function (event) {
      event.preventDefault();
      depth += 1;
      zone.classList.add("is-hot");
    });
    zone.addEventListener("dragover", function (event) {
      event.preventDefault();
    });
    zone.addEventListener("dragleave", function (event) {
      event.preventDefault();
      depth -= 1;
      if (depth <= 0) {
        depth = 0;
        zone.classList.remove("is-hot");
      }
    });
    zone.addEventListener("drop", function (event) {
      event.preventDefault();
      depth = 0;
      zone.classList.remove("is-hot");
      var files = event.dataTransfer && event.dataTransfer.files;
      if (!files || !files.length) {
        return;
      }
      try {
        fileInput.files = files;
      } catch {
        return;
      }
      showName();
    });
  }

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!target || !target.closest) {
      return;
    }
    var button = target.closest("[data-reupload]");
    if (!button) {
      return;
    }
    var drop = document.querySelector("[data-dropzone]");
    if (!drop) {
      return;
    }
    drop.scrollIntoView({ block: "center" });
    drop.classList.add("is-highlight");
  });

  // 已过期录音会话的删除（LZW-62 D 节）。只有过期行带 data-delete-session。
  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!target || !target.closest) {
      return;
    }
    var button = target.closest("[data-delete-session]");
    if (!button) {
      return;
    }
    var sessionId = button.getAttribute("data-delete-session") || "";
    if (!/^[0-9a-f]{32}$/.test(sessionId)) {
      return;
    }
    if (!window.confirm("删除这个已过期的录音会话？服务端切片会一起清理。")) {
      return;
    }
    fetch("/recordings/" + sessionId, { method: "DELETE" })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("删除失败");
        }
        var row = button.closest("[data-session-row]");
        if (row) {
          row.remove();
        }
      })
      .catch(function () {
        window.alert("删除失败，请稍后重试");
      });
  });

  var live = document.getElementById("live");
  if (!live || !live.hasAttribute("data-live-poll")) {
    return;
  }

  var delay = 5000;

  function schedule(ok) {
    if (ok) {
      delay = 5000;
    } else {
      delay = Math.min(delay * 2, 60000);
    }
    window.setTimeout(tick, delay);
  }

  function syncLive(current, nextLive) {
    var slot = current.querySelector("#fail-slot");
    var nextBanner = nextLive.querySelector(".fail-banner");
    var currentBanner = current.querySelector(".fail-banner");
    if (nextBanner && currentBanner) {
      currentBanner.replaceWith(nextBanner.cloneNode(true));
      if (slot) {
        slot.hidden = false;
      }
    } else if (nextBanner) {
      if (slot) {
        slot.replaceChildren(nextBanner.cloneNode(true));
        slot.hidden = false;
      }
    } else if (currentBanner) {
      if (slot) {
        slot.replaceChildren();
        slot.hidden = true;
      } else {
        currentBanner.remove();
      }
    }
    var nextRecent = nextLive.querySelector(".recent");
    var currentRecent = current.querySelector(".recent");
    if (nextRecent && currentRecent) {
      var clones = Array.from(nextRecent.childNodes).map(function (node) {
        return node.cloneNode(true);
      });
      currentRecent.replaceChildren(...clones);
    }
  }

  function tick() {
    var url = window.location.pathname + window.location.search;
    window.fetch(url, { headers: { Accept: "text/html" }, credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("poll");
        }
        return response.text();
      })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        var nextLive = doc.getElementById("live");
        if (!nextLive) {
          // 没有 #live 按失败退避，不要把 delay 打回 5 秒。
          schedule(false);
          return;
        }
        var current = document.getElementById("live");
        if (current) {
          syncLive(current, nextLive);
        }
        var usage = document.getElementById("usage");
        var nextUsage = doc.getElementById("usage");
        if (usage && nextUsage) {
          usage.textContent = nextUsage.textContent;
        }
        if (nextLive.hasAttribute("data-live-poll")) {
          schedule(true);
        }
      })
      .catch(function () {
        schedule(false);
      });
  }

  window.setTimeout(tick, delay);
})();

(function () {
  var player = document.getElementById("player");
  if (!player) {
    return;
  }
  var audio = player.querySelector("audio");
  if (!audio) {
    return;
  }

  var playBtn = document.getElementById("play");
  var iconPlay = playBtn ? playBtn.querySelector(".icon-play") : null;
  var iconPause = playBtn ? playBtn.querySelector(".icon-pause") : null;
  var speedBtn = document.getElementById("speed");
  var timeEl = document.getElementById("player-time");
  var currSpan = timeEl ? timeEl.querySelector(".curr") : null;
  var totalSpan = timeEl ? timeEl.querySelector(".total") : null;
  var wave = document.getElementById("wave");
  var head = document.getElementById("head");
  var track = document.getElementById("player-track");
  var skipBtns = player.querySelectorAll("[data-skip]");
  var segLines = Array.from(document.querySelectorAll(".seg-line[data-start]"));

  var speeds = [1.0, 1.25, 1.5, 2.0, 0.75];
  var speedLabels = ["1.0x", "1.25x", "1.5x", "2.0x", "0.75x"];
  var speedIdx = 0;

  if (speedBtn) {
    speedBtn.addEventListener("click", function () {
      speedIdx = (speedIdx + 1) % speeds.length;
      var rate = speeds[speedIdx];
      audio.playbackRate = rate;
      speedBtn.textContent = speedLabels[speedIdx];
    });
  }

  function formatTime(sec) {
    if (isNaN(sec) || sec < 0) {
      sec = 0;
    }
    var s = Math.floor(sec);
    var m = Math.floor(s / 60);
    s = s % 60;
    return m + ":" + (s < 10 ? "0" + s : s);
  }

  function setPlayState(isPlaying) {
    if (playBtn) {
      playBtn.setAttribute("aria-label", isPlaying ? "暂停" : "播放");
      if (iconPlay) {
        iconPlay.style.display = isPlaying ? "none" : "";
      }
      if (iconPause) {
        iconPause.style.display = isPlaying ? "" : "none";
      }
    }
  }

  // 失败处理只做「终局才定罪、定了也可重试」：播放器容器、audio 与按钮一律保留，
  // 只在播放器内追加一条可重试提示。瞬时信号（单个 source/audio 的 error、init 中间值、
  // AbortError、NotAllowedError）只记信号，不呈现失败；canplay/playing 到达自动清除。
  var playbackFailed = false;
  var failureNote = null;

  function clearPlaybackFailure() {
    playbackFailed = false;
    if (failureNote && failureNote.parentNode) {
      failureNote.parentNode.removeChild(failureNote);
    }
    failureNote = null;
  }

  function renderPlaybackFailure() {
    if (playbackFailed) {
      return;
    }
    playbackFailed = true;
    failureNote = document.createElement("p");
    failureNote.className = "muted player-failure";
    failureNote.setAttribute("data-player-failure", "");
    failureNote.setAttribute("role", "status");
    var text = document.createElement("span");
    text.textContent = "音频文件不存在或格式不可用";
    failureNote.appendChild(text);
    var retryBtn = document.createElement("button");
    retryBtn.className = "btn";
    retryBtn.type = "button";
    retryBtn.setAttribute("data-player-retry", "");
    retryBtn.textContent = "重试";
    retryBtn.addEventListener("click", retryPlayback);
    failureNote.appendChild(retryBtn);
    player.appendChild(failureNote);
  }

  // 终局复核（只在信号到达或用户明确尝试播放时调用，初始化阶段不调用）：
  // ①全部 source 已失败且此刻仍无可用源，或 audio 处于非 MEDIA_ERR_ABORTED(1) 的终局错误；
  // ②play() 被非瞬时名义拒绝（见 handlePlayRejection）。
  var sourceNodes = audio.querySelectorAll("source");
  var sourceFailures = 0;

  function hasTerminalAudioFailure() {
    var err = audio.error;
    if (err) {
      // MEDIA_ERR_ABORTED：被刷新/重连/load 打断，不定罪。
      return err.code !== 1;
    }
    if (audio.networkState !== 3) {
      return false;
    }
    return sourceNodes.length === 0 || sourceFailures >= sourceNodes.length;
  }

  // 只处理非瞬时名义的 play() 拒绝：AbortError（被打断）与 NotAllowedError
  // （缺用户手势）一律忽略，直接等用户再点。
  function handlePlayRejection(err) {
    if (!err || !err.name) {
      return;
    }
    if (err.name === "AbortError" || err.name === "NotAllowedError") {
      return;
    }
    if (err.name === "NotSupportedError") {
      renderPlaybackFailure();
    }
  }

  function attemptPlay() {
    var playing = audio.play();
    if (playing && playing.catch) {
      playing.catch(handlePlayRejection);
    }
  }

  function retryPlayback() {
    clearPlaybackFailure();
    sourceFailures = 0;
    audio.load();
    attemptPlay();
  }

  // audio 自身的 error 只作信号汇入终局复核，不再直达失败呈现。
  audio.addEventListener("error", function () {
    if (hasTerminalAudioFailure()) {
      renderPlaybackFailure();
    }
  });

  sourceNodes.forEach(function (source) {
    source.addEventListener("error", function () {
      sourceFailures += 1;
      if (hasTerminalAudioFailure()) {
        renderPlaybackFailure();
      }
    });
  });

  audio.addEventListener("loadstart", function () {
    sourceFailures = 0;
  });

  // 源协商成功即证明音频可播：自动清除失败呈现与内部标记，不要求再点一次。
  audio.addEventListener("canplay", clearPlaybackFailure);
  audio.addEventListener("playing", clearPlaybackFailure);

  if (playBtn) {
    playBtn.addEventListener("click", function () {
      if (playbackFailed) {
        // 已进入失败呈现：本次点击即重试，重新加载后再播。
        retryPlayback();
        return;
      }
      if (audio.paused) {
        attemptPlay();
      } else {
        audio.pause();
      }
    });
  }

  audio.addEventListener("play", function () {
    setPlayState(true);
  });

  audio.addEventListener("pause", function () {
    setPlayState(false);
  });

  skipBtns.forEach(function (btn) {
    btn.addEventListener("click", function () {
      var delta = parseFloat(btn.getAttribute("data-skip")) || 0;
      var dur = getDuration();
      audio.currentTime = Math.max(0, Math.min(dur || 0, audio.currentTime + delta));
      syncProgress();
    });
  });

  var BAR_COUNT = 140;
  var waveBars = [];
  if (wave) {
    var seed = 11;
    for (var i = 0; i < BAR_COUNT; i++) {
      seed = (seed * 9301 + 49297) % 233280;
      var h = 22 + Math.abs(Math.sin(i * 0.31)) * 44 + (seed / 233280) * 30;
      var bar = document.createElement("i");
      bar.style.height = Math.min(Math.round(h), 100) + "%";
      wave.appendChild(bar);
      waveBars.push(bar);
    }
  }

  function getDuration() {
    return (audio.duration && !isNaN(audio.duration) && audio.duration > 0)
      ? audio.duration
      : (parseFloat(player.getAttribute("data-duration")) || 0);
  }

  function updateWaveAndHead(pct) {
    var cut = Math.round(BAR_COUNT * pct);
    for (var i = 0; i < waveBars.length; i++) {
      if (i < cut) {
        waveBars[i].classList.add("p");
      } else {
        waveBars[i].classList.remove("p");
      }
    }
    if (head) {
      head.style.left = "calc(" + (pct * 100) + "% - 1px)";
    }
  }

  function updateHighlight(time) {
    var activeSeg = null;
    for (var i = 0; i < segLines.length; i++) {
      var s = parseFloat(segLines[i].getAttribute("data-start")) || 0;
      var e = parseFloat(segLines[i].getAttribute("data-end")) || 0;
      if (time >= s && (time < e || i === segLines.length - 1)) {
        activeSeg = segLines[i];
        break;
      }
    }
    for (var j = 0; j < segLines.length; j++) {
      if (segLines[j] === activeSeg) {
        segLines[j].classList.add("now");
      } else {
        segLines[j].classList.remove("now");
      }
    }
  }

  function syncProgress() {
    var dur = getDuration();
    var cur = audio.currentTime || 0;
    if (currSpan) {
      currSpan.textContent = formatTime(cur);
    }
    if (totalSpan && dur > 0) {
      totalSpan.textContent = "/ " + formatTime(dur);
    }
    var pct = dur > 0 ? Math.min(Math.max(cur / dur, 0), 1) : 0;
    updateWaveAndHead(pct);
    updateHighlight(cur);
  }

  audio.addEventListener("timeupdate", syncProgress);
  audio.addEventListener("loadedmetadata", syncProgress);
  audio.addEventListener("durationchange", syncProgress);

  function seekFromEvent(e) {
    if (!track) return;
    var rect = track.getBoundingClientRect();
    if (rect.width <= 0) return;
    var x = e.clientX - rect.left;
    var pct = Math.max(0, Math.min(1, x / rect.width));
    var dur = getDuration();
    if (dur > 0) {
      audio.currentTime = pct * dur;
      syncProgress();
    }
  }

  if (track) {
    track.addEventListener("click", seekFromEvent);
  }

  segLines.forEach(function (seg) {
    var startVal = parseFloat(seg.getAttribute("data-start")) || 0;
    var tsBtn = seg.querySelector(".ts");
    var target = tsBtn || seg;
    target.addEventListener("click", function (e) {
      e.stopPropagation();
      audio.currentTime = startVal;
      syncProgress();
    });
  });

  syncProgress();
})();


(function () {
  // 改名：列表页文件列与详情页标题旁铅笔。回车=保存、Esc=取消；
  // 校验失败留在编辑态并在本行提示，不落库。写入只走 textContent。
  var scopes = document.querySelectorAll("[data-rename-scope]");
  if (!scopes.length) return;

  var MAX_CHARS = 120;

  function showError(scope, message) {
    var error = scope.querySelector("[data-rename-error]");
    if (!error) return;
    error.textContent = message;
    error.hidden = message === "";
  }

  function currentName(scope) {
    var nameEl = scope.querySelector(".file-name") || scope.querySelector(".page-title");
    return nameEl ? nameEl.textContent.trim() : "";
  }

  function setEditing(scope, editing) {
    var editor = scope.querySelector("[data-rename-editor]");
    var nameEl = scope.querySelector(".file-name") || scope.querySelector(".page-title");
    var openBtn = scope.querySelector("[data-rename-open]");
    if (editor) {
      editor.hidden = !editing;
      if (editing) {
        var input = editor.querySelector("[data-rename-input]");
        if (input) {
          input.value = currentName(scope);
          input.focus();
          input.select();
        }
      }
    }
    if (nameEl) nameEl.hidden = editing;
    if (openBtn) openBtn.hidden = editing;
    if (!editing) showError(scope, "");
  }

  function localCheck(value) {
    if (!value.trim()) return "名称不能为空";
    if (Array.from(value.trim()).length > MAX_CHARS) return "名称最多 " + MAX_CHARS + " 个字符";
    return "";
  }

  function save(scope) {
    var editor = scope.querySelector("[data-rename-editor]");
    if (!editor) return;
    var jobId = editor.getAttribute("data-job-id") || "";
    var input = editor.querySelector("[data-rename-input]");
    var raw = input ? input.value : "";
    var problem = localCheck(raw);
    if (problem) {
      showError(scope, problem);
      return;
    }
    showError(scope, "");
    fetch("/jobs/" + encodeURIComponent(jobId) + "/rename", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ name: raw })
    })
      .then(function (response) {
        return response.json().then(function (payload) {
          return { ok: response.ok, payload: payload };
        });
      })
      .then(function (result) {
        if (!result.ok) {
          var message = result.payload && result.payload.error ? result.payload.error : "改名失败";
          showError(scope, message);
          return;
        }
        var nameEl = scope.querySelector(".file-name") || scope.querySelector(".page-title");
        if (nameEl) nameEl.textContent = result.payload.source_name;
        setEditing(scope, false);
      })
      .catch(function () {
        showError(scope, "改名失败，请重试");
      });
  }

  scopes.forEach(function (scope) {
    var openBtn = scope.querySelector("[data-rename-open]");
    var editor = scope.querySelector("[data-rename-editor]");
    var saveBtn = scope.querySelector("[data-rename-save]");
    var cancelBtn = scope.querySelector("[data-rename-cancel]");
    var input = scope.querySelector("[data-rename-input]");
    if (openBtn) {
      openBtn.addEventListener("click", function () {
        setEditing(scope, true);
      });
    }
    if (saveBtn) {
      saveBtn.addEventListener("click", function () {
        save(scope);
      });
    }
    if (cancelBtn) {
      cancelBtn.addEventListener("click", function () {
        setEditing(scope, false);
      });
    }
    if (input) {
      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter") {
          e.preventDefault();
          save(scope);
        } else if (e.key === "Escape") {
          e.preventDefault();
          setEditing(scope, false);
        }
      });
    }
    if (editor) setEditing(scope, false);
  });
})();
