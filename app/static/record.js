/* 在线录音（LZW-62）。口径见 docs/SPEC-BATCH-20261103.md D 节。
   要点：
   - 每片都是一次独立 start()/stop() 的完整容器（timeslice 分片不能独立解码），
     服务端才能逐段解码为 WAV 再拼接；
   - 先存 IndexedDB，服务端回执后才删；断网/关页重开自动补传；
   - 上限与提示阈值只读服务端下发的 data-*（来自 env），请求参数改不动。 */
(function () {
  "use strict";

  var root = document.querySelector("[data-record]");
  if (!root) {
    return;
  }

  var DB_NAME = "transcriber-record";
  var DB_VERSION = 1;
  var STORE_PARTS = "parts";
  var STORE_SESSIONS = "sessions";

  var cfg = {
    prefillName: root.getAttribute("data-prefill-name") || "",
    maxSeconds: parseFloat(root.getAttribute("data-max-seconds")) || 3600,
    partSeconds: parseFloat(root.getAttribute("data-part-seconds")) || 5,
    maxParts: parseInt(root.getAttribute("data-max-parts"), 10) || 720,
    warnSeconds: (root.getAttribute("data-warn-seconds") || "300,60")
      .split(",")
      .map(function (item) {
        return parseFloat(item);
      })
      .filter(function (item) {
        return isFinite(item) && item > 0;
      })
      .sort(function (a, b) {
        return b - a;
      })
  };

  var el = {
    insecure: root.querySelector("[data-insecure]"),
    error: root.querySelector("[data-error]"),
    setup: root.querySelector("[data-setup]"),
    live: root.querySelector("[data-live]"),
    name: root.querySelector("[data-name]"),
    start: root.querySelector("[data-start]"),
    startLabel: root.querySelector("[data-start-label]"),
    timer: root.querySelector("[data-timer]"),
    warn: root.querySelector("[data-warn]"),
    wave: root.querySelector("[data-wave]"),
    saved: root.querySelector("[data-saved]"),
    pending: root.querySelector("[data-pending]"),
    state: root.querySelector("[data-state]"),
    pause: root.querySelector("[data-pause]"),
    stop: root.querySelector("[data-stop]"),
    back: root.querySelector("[data-back]")
  };

  var state = "idle"; // idle | recording | paused | finishing | done
  var sessionId = null;
  var tier = "fast";
  var partIndex = 0;
  var savedParts = 0;
  var elapsed = 0;
  var activeSince = 0;
  var warned = {};
  var recorder = null;
  var segmentTimer = null;
  var stream = null;
  var audioCtx = null;
  var analyser = null;
  var waveRaf = null;
  var tickTimer = null;
  var queue = [];
  var queueBusy = false;
  var finishAfterFlush = false;
  var secureOk = window.isSecureContext === true;

  /* ---------- IndexedDB ---------- */

  function openDb() {
    return new Promise(function (resolve, reject) {
      var request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = function () {
        var db = request.result;
        if (!db.objectStoreNames.contains(STORE_PARTS)) {
          db.createObjectStore(STORE_PARTS, { keyPath: "key" });
        }
        if (!db.objectStoreNames.contains(STORE_SESSIONS)) {
          db.createObjectStore(STORE_SESSIONS, { keyPath: "id" });
        }
      };
      request.onsuccess = function () {
        resolve(request.result);
      };
      request.onerror = function () {
        reject(request.error);
      };
    });
  }

  function idb(storeName, mode, work) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction(storeName, mode);
        var store = tx.objectStore(storeName);
        var result;
        try {
          result = work(store);
        } catch (error) {
          reject(error);
          return;
        }
        tx.oncomplete = function () {
          resolve(result && result.result !== undefined ? result.result : result);
        };
        tx.onerror = function () {
          reject(tx.error);
        };
        tx.onabort = function () {
          reject(tx.error);
        };
      });
    });
  }

  function partKey(id, index) {
    return id + ":" + index;
  }

  function dbPutPart(item) {
    return idb(STORE_PARTS, "readwrite", function (store) {
      store.put(item);
    });
  }

  function dbDeletePart(id, index) {
    return idb(STORE_PARTS, "readwrite", function (store) {
      store.delete(partKey(id, index));
    });
  }

  function dbParts(id) {
    return idb(STORE_PARTS, "readonly", function (store) {
      return store.getAll();
    }).then(function (rows) {
      return (rows || [])
        .filter(function (row) {
          return row && row.session_id === id;
        })
        .sort(function (a, b) {
          return a.index - b.index;
        });
    });
  }

  function dbSaveSession(record) {
    return idb(STORE_SESSIONS, "readwrite", function (store) {
      store.put(record);
    });
  }

  function dbSessions() {
    return idb(STORE_SESSIONS, "readonly", function (store) {
      return store.getAll();
    }).then(function (rows) {
      return rows || [];
    });
  }

  function dbClearSession(id) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction([STORE_PARTS, STORE_SESSIONS], "readwrite");
        tx.objectStore(STORE_SESSIONS).delete(id);
        var parts = tx.objectStore(STORE_PARTS).getAll();
        parts.onsuccess = function () {
          (parts.result || []).forEach(function (row) {
            if (row && row.session_id === id) {
              tx.objectStore(STORE_PARTS).delete(row.key);
            }
          });
        };
        tx.oncomplete = function () {
          resolve(true);
        };
        tx.onerror = function () {
          reject(tx.error);
        };
      });
    });
  }

  /* ---------- 小工具 ---------- */

  function showError(text) {
    if (!el.error) {
      return;
    }
    if (text) {
      el.error.textContent = text;
      el.error.hidden = false;
    } else {
      el.error.textContent = "";
      el.error.hidden = true;
    }
  }

  function showWarn(text) {
    if (!el.warn) {
      return;
    }
    if (text) {
      el.warn.textContent = text;
      el.warn.hidden = false;
    } else {
      el.warn.textContent = "";
      el.warn.hidden = true;
    }
  }

  function fmtClock(seconds) {
    var total = Math.max(0, Math.floor(seconds));
    var hh = Math.floor(total / 3600);
    var mm = Math.floor((total % 3600) / 60);
    var ss = total % 60;
    if (hh > 0) {
      return hh + ":" + ("0" + mm).slice(-2) + ":" + ("0" + ss).slice(-2);
    }
    return ("0" + mm).slice(-2) + ":" + ("0" + ss).slice(-2);
  }

  function setState(text) {
    if (el.state) {
      el.state.textContent = text;
    }
  }

  function refreshCounters() {
    if (el.saved) {
      el.saved.textContent = String(savedParts);
    }
    if (el.pending) {
      el.pending.textContent = String(queue.length);
    }
  }

  function currentTier() {
    var picked = root.querySelector('input[name="record-tier"]:checked');
    return picked ? picked.value : "fast";
  }

  function currentName() {
    return el.name && el.name.value ? el.name.value.trim() : "";
  }

  function busyDirty() {
    return state === "recording" || state === "paused" || state === "finishing" || queue.length > 0;
  }

  /* ---------- 上传队列 ---------- */

  function enqueue(item) {
    queue.push(item);
    refreshCounters();
    pump();
  }

  function pump() {
    if (queueBusy || !sessionId) {
      return;
    }
    if (queue.length === 0) {
      if (finishAfterFlush) {
        finishAfterFlush = false;
        doFinish();
      }
      return;
    }
    queueBusy = true;
    var item = queue[0];
    uploadPart(item)
      .then(function (body) {
        if (typeof body.saved_parts === "number") {
          savedParts = body.saved_parts;
        }
        refreshCounters();
        queue.shift();
        queueBusy = false;
        return dbDeletePart(sessionId, item.index);
      })
      .then(function () {
        refreshCounters();
        pump();
      })
      .catch(function () {
        queueBusy = false;
        refreshCounters();
        setTimeout(pump, retryDelay());
      });
  }

  var retryStep = 0;

  function retryDelay() {
    retryStep = Math.min(retryStep + 1, 6);
    return Math.min(1000 * Math.pow(2, retryStep - 1), 15000);
  }

  function uploadPart(item) {
    return fetch("/recordings/" + sessionId + "/parts/" + item.index, {
      method: "POST",
      headers: {
        "content-type": "application/octet-stream",
        "x-part-duration": String(item.duration)
      },
      body: item.blob
    }).then(function (response) {
      return response.json().then(function (body) {
        if (!response.ok) {
          throw new Error((body && body.error) || "上传失败");
        }
        retryStep = 0;
        return body;
      });
    });
  }

  /* ---------- 录音 ---------- */

  function pickMime() {
    var candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"];
    if (typeof MediaRecorder === "undefined") {
      return "";
    }
    for (var i = 0; i < candidates.length; i += 1) {
      if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(candidates[i])) {
        return candidates[i];
      }
    }
    return "";
  }

  function startSegment() {
    if (!stream || state !== "recording") {
      return;
    }
    var mime = pickMime();
    var options = mime ? { mimeType: mime } : {};
    var chunks = [];
    var rec;
    try {
      rec = new MediaRecorder(stream, options);
    } catch (error) {
      showError("无法开始录音：" + (error && error.message ? error.message : "未知错误"));
      return;
    }
    rec.ondataavailable = function (event) {
      if (event.data && event.data.size > 0) {
        chunks.push(event.data);
      }
    };
    rec.onstop = function () {
      if (segmentTimer) {
        clearTimeout(segmentTimer);
        segmentTimer = null;
      }
      var blob = new Blob(chunks, { type: rec.mimeType || mime || "audio/webm" });
      chunks = [];
      if (rec === recorder) {
        recorder = null;
      }
      if (blob.size > 0 && sessionId) {
        var index = partIndex;
        partIndex += 1;
        var duration = Math.min(cfg.partSeconds * 1.5, cfg.partSeconds);
        var item = {
          key: partKey(sessionId, index),
          session_id: sessionId,
          index: index,
          duration: duration,
          blob: blob
        };
        dbPutPart(item)
          .then(function () {
            enqueue(item);
          })
          .catch(function () {
            enqueue(item);
          });
      }
      if (state === "recording") {
        startSegment();
      }
    };
    rec.start();
    recorder = rec;
    segmentTimer = setTimeout(function () {
      if (rec === recorder && rec.state === "recording") {
        rec.stop();
      }
    }, cfg.partSeconds * 1000);
  }

  function stopSegment() {
    if (segmentTimer) {
      clearTimeout(segmentTimer);
      segmentTimer = null;
    }
    if (recorder && recorder.state !== "inactive") {
      try {
        recorder.stop();
      } catch (error) {
        recorder = null;
      }
    } else {
      recorder = null;
    }
  }

  function tick() {
    if (state === "recording") {
      elapsed = elapsed + (performance.now() - activeSince) / 1000;
      activeSince = performance.now();
    }
    if (el.timer) {
      el.timer.textContent = fmtClock(elapsed);
    }
    checkWarnings();
    if (elapsed >= cfg.maxSeconds && state === "recording") {
      showWarn("已到 1 小时上限，自动结束并送转写");
      stopAndFinish(true);
    }
  }

  function checkWarnings() {
    var remaining = cfg.maxSeconds - elapsed;
    cfg.warnSeconds.forEach(function (threshold) {
      if (remaining <= threshold && !warned[threshold]) {
        warned[threshold] = true;
        showWarn("剩余 " + humanSpan(threshold));
      }
    });
  }

  function humanSpan(seconds) {
    if (seconds >= 60) {
      var minutes = Math.round(seconds / 60);
      return minutes + " 分钟";
    }
    return Math.round(seconds) + " 秒";
  }

  function startTick() {
    if (tickTimer) {
      return;
    }
    tickTimer = setInterval(tick, 200);
  }

  function stopTick() {
    if (tickTimer) {
      clearInterval(tickTimer);
      tickTimer = null;
    }
  }

  /* ---------- 波形 ---------- */

  function startWave() {
    if (!el.wave || !stream || !window.AudioContext) {
      return;
    }
    try {
      audioCtx = new window.AudioContext();
      var source = audioCtx.createMediaStreamSource(stream);
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = 1024;
      source.connect(analyser);
    } catch (error) {
      return;
    }
    var ctx = el.wave.getContext("2d");
    var buffer = new Uint8Array(analyser.frequencyBinCount);
    var draw = function () {
      waveRaf = window.requestAnimationFrame(draw);
      analyser.getByteTimeDomainData(buffer);
      var width = el.wave.width;
      var height = el.wave.height;
      ctx.clearRect(0, 0, width, height);
      ctx.lineWidth = 2;
      ctx.strokeStyle = "#1d5fbf";
      ctx.beginPath();
      // 时域缓冲（fftSize/2 点，默认 512）通常少于画布宽度（模板 720px），
      // 必须按比例取样，否则 x≥buffer.length 的下标越界会被兜底值填成中线，
      // 画布右侧整段恒平（LZW-94）。映射对任意缓冲长度 × 任意画布宽度成立。
      for (var x = 0; x < width; x += 1) {
        var idx = width > 0 ? Math.floor(x * buffer.length / width) : 0;
        if (idx >= buffer.length) {
          idx = buffer.length - 1;
        }
        var sample = buffer[idx];
        var value = typeof sample === "number" && !isNaN(sample) ? sample : 128;
        var y = (value / 128 - 1) * (height / 2) + height / 2;
        if (x === 0) {
          ctx.moveTo(x, y);
        } else {
          ctx.lineTo(x, y);
        }
      }
      ctx.stroke();
    };
    draw();
  }

  function stopWave() {
    if (waveRaf) {
      window.cancelAnimationFrame(waveRaf);
      waveRaf = null;
    }
    if (audioCtx) {
      audioCtx.close();
      audioCtx = null;
      analyser = null;
    }
    if (el.wave) {
      var ctx = el.wave.getContext("2d");
      ctx.clearRect(0, 0, el.wave.width, el.wave.height);
    }
  }

  /* ---------- 会话 ---------- */

  function createSession() {
    return fetch("/recordings", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ tier: currentTier(), name: currentName() })
    }).then(function (response) {
      return response.json().then(function (body) {
        if (!response.ok) {
          throw new Error((body && body.error) || "无法创建录音会话");
        }
        return body;
      });
    });
  }

  function startRecording() {
    if (!secureOk) {
      return;
    }
    showError("");
    showWarn("");
    el.start.disabled = true;
    if (el.startLabel) {
      el.startLabel.textContent = "准备中…";
    }
    tier = currentTier();
    requestMic()
      .then(function (micStream) {
        stream = micStream;
        return createSession();
      })
      .then(function (body) {
        sessionId = body.session_id;
        partIndex = 0;
        savedParts = 0;
        elapsed = 0;
        warned = {};
        activeSince = performance.now();
        state = "recording";
        el.setup.hidden = true;
        el.live.hidden = false;
        setState("录音中");
        refreshCounters();
        if (el.pause) {
          el.pause.textContent = "暂停";
        }
        startWave();
        startTick();
        startSegment();
        return dbSaveSession({
          id: sessionId,
          tier: tier,
          name: currentName(),
          finished: false,
          created_at: new Date().toISOString()
        });
      })
      .catch(function (error) {
        releaseMic();
        state = "idle";
        el.start.disabled = false;
        if (el.startLabel) {
          el.startLabel.textContent = "开始录音";
        }
        showError(micMessage(error));
      });
  }

  function requestMic() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return Promise.reject({ name: "NotSupportedError" });
    }
    return navigator.mediaDevices.getUserMedia({ audio: true });
  }

  function releaseMic() {
    if (stream) {
      stream.getTracks().forEach(function (track) {
        track.stop();
      });
      stream = null;
    }
    stopWave();
  }

  function micMessage(error) {
    var name = error && error.name ? error.name : "";
    if (name === "NotAllowedError" || name === "SecurityError") {
      return "麦克风权限被拒绝。请在浏览器地址栏允许麦克风后重试，或返回工作台。";
    }
    if (name === "NotFoundError" || name === "DevicesNotFoundError") {
      return "没有找到可用的麦克风设备，请接入麦克风后重试，或返回工作台。";
    }
    if (name === "NotSupportedError") {
      return "当前浏览器不支持录音，请改用支持录音的浏览器。";
    }
    return "无法开始录音：" + (error && error.message ? error.message : "未知错误");
  }

  function togglePause() {
    if (state === "recording") {
      state = "paused";
      elapsed = elapsed + (performance.now() - activeSince) / 1000;
      stopSegment();
      setState("已暂停");
      if (el.pause) {
        el.pause.textContent = "继续";
      }
    } else if (state === "paused") {
      state = "recording";
      activeSince = performance.now();
      setState("录音中");
      if (el.pause) {
        el.pause.textContent = "暂停";
      }
      // 断网重开后续录：麦克风已随页面释放，继续时要重新申请（I3）。
      if (!stream) {
        requestMic()
          .then(function (micStream) {
            stream = micStream;
            if (state === "recording") {
              startWave();
              startSegment();
            }
          })
          .catch(function (error) {
            state = "paused";
            setState("已暂停");
            if (el.pause) {
              el.pause.textContent = "继续";
            }
            showError(micMessage(error));
          });
        return;
      }
      startSegment();
    }
  }

  function stopAndFinish(automatic) {
    if (state === "finishing" || state === "done") {
      return;
    }
    if (state === "recording") {
      elapsed = elapsed + (performance.now() - activeSince) / 1000;
    }
    stopSegment();
    releaseMic();
    stopTick();
    state = "finishing";
    setState(automatic ? "到限，正在收尾" : "正在结束");
    if (el.pause) {
      el.pause.disabled = true;
    }
    if (el.stop) {
      el.stop.disabled = true;
    }
    finishAfterFlush = true;
    pump();
  }

  function doFinish() {
    if (!sessionId) {
      return;
    }
    fetch("/recordings/" + sessionId + "/finish", {
      method: "POST",
      headers: { "content-type": "application/json" },
      // expected_parts：采集端自报的切片总数，服务端据此查出「缺尾片」（I4）。
      body: JSON.stringify({
        tier: tier,
        name: currentName(),
        expected_parts: partIndex
      })
    })
      .then(function (response) {
        return response.json().then(function (body) {
          if (!response.ok) {
            throw new Error((body && body.error) || "结束录音失败");
          }
          return body;
        });
      })
      .then(function (body) {
        state = "done";
        var finishedId = sessionId;
        sessionId = null;
        return dbClearSession(finishedId).then(function () {
          var jobId = String((body && body.job_id) || "");
          if (!/^[0-9a-f]{32}$/.test(jobId)) {
            showError("结束录音返回的任务 id 不合法，请回工作台查看");
            return;
          }
          window.location.assign("/jobs/" + jobId + "/view");
        });
      })
      .catch(function (error) {
        state = "paused";
        finishAfterFlush = false;
        if (el.pause) {
          el.pause.disabled = false;
          el.pause.textContent = "继续";
        }
        if (el.stop) {
          el.stop.disabled = false;
        }
        setState("待补传");
        showError(
          "结束失败：" +
            (error && error.message ? error.message : "未知错误") +
            "。切片已保存在本地，回到本页会自动补传。"
        );
      });
  }

  /* ---------- 关页重开续录 ---------- */

  function resumeExisting() {
    return dbSessions().then(function (rows) {
      var pending = (rows || []).filter(function (row) {
        return row && !row.finished;
      });
      if (pending.length === 0) {
        return null;
      }
      var latest = pending.sort(function (a, b) {
        return String(b.created_at).localeCompare(String(a.created_at));
      })[0];
      return fetch("/recordings/" + latest.id)
        .then(function (response) {
          if (!response.ok) {
            return null;
          }
          return response.json();
        })
        .then(function (body) {
          if (!body || body.status === "done") {
            return dbClearSession(latest.id).then(function () {
              return null;
            });
          }
          sessionId = latest.id;
          tier = body.tier || latest.tier || "fast";
          savedParts = body.saved_parts || 0;
          state = "paused";
          el.setup.hidden = true;
          el.live.hidden = false;
          setState("待补传");
          if (el.pause) {
            el.pause.textContent = "继续";
          }
          refreshCounters();
          return dbParts(sessionId).then(function (items) {
            // I3：下一号取「服务端已存片」与「本地待传片」的合并最大值 +1，
            // 否则续录会撞上本地尚未上传的片号，把待传片覆盖掉。
            var localNext = 0;
            items.forEach(function (item) {
              localNext = Math.max(localNext, (item.index || 0) + 1);
            });
            partIndex = Math.max(body.next_index || 0, localNext);
            items.forEach(function (item) {
              queue.push(item);
            });
            refreshCounters();
            pump();
            return null;
          });
        });
    });
  }

  /* ---------- 离开确认 ---------- */

  function confirmLeave() {
    if (!busyDirty()) {
      return true;
    }
    var text = "离开会暂停录音，已录切片保留，回到本页可继续录或结束转写。确定离开吗？";
    if (!window.confirm(text)) {
      return false;
    }
    if (state === "recording") {
      state = "paused";
      elapsed = elapsed + (performance.now() - activeSince) / 1000;
      stopSegment();
      releaseMic();
      stopTick();
      setState("已暂停");
    }
    return true;
  }

  function onBeforeUnload(event) {
    if (!busyDirty()) {
      return undefined;
    }
    event.preventDefault();
    event.returnValue = "录音未结束";
    return "录音未结束";
  }

  /* ---------- 绑定 ---------- */

  function bind() {
    if (!secureOk) {
      if (el.insecure) {
        el.insecure.hidden = false;
      }
      if (el.start) {
        el.start.disabled = true;
        el.start.setAttribute("aria-disabled", "true");
      }
      return;
    }
    if (el.start) {
      el.start.disabled = false;
      el.start.addEventListener("click", startRecording);
    }
    if (el.pause) {
      el.pause.addEventListener("click", togglePause);
    }
    if (el.stop) {
      el.stop.addEventListener("click", function () {
        stopAndFinish(false);
      });
    }
    if (el.back) {
      el.back.addEventListener("click", function (event) {
        if (!confirmLeave()) {
          event.preventDefault();
        }
      });
    }
    window.addEventListener("beforeunload", onBeforeUnload);
    window.addEventListener("online", pump);
    resumeExisting();
  }

  bind();
})();
