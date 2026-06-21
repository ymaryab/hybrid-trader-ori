/* Hybrid Trade panel — Phantom (Solana) + dashboard */
(function () {
  "use strict";

  const CHAINS = {
    "0x2105": { name: "Base", rpc: "https://mainnet.base.org", key: "base" },
    "0xa4b1": { name: "Arbitrum", rpc: "https://arb1.arbitrum.io/rpc", key: "arbitrum" },
    "0x38": { name: "BSC", rpc: "https://bsc-dataseed.binance.org", key: "bsc" },
  };
  const CHAIN_LABEL = {
    solana: "Solana",
    base: "Base",
    arbitrum: "Arbitrum",
    bsc: "BSC",
  };
  const CHAIN_TO_ID = { base: "0x2105", arbitrum: "0xa4b1", bsc: "0x38" };

  let phantomAddress = localStorage.getItem("phantomAddress") || "";
  let selectedChain = localStorage.getItem("selectedChain") || "solana";
  let eip6963Providers = [];
  let brainPollTimer = null;
  let brainKickAttempts = 0;
  var brainKickMax = 12;
  var phantomBusy = false;
  var entryChains = ["solana"];

  function fetchWithTimeout(url, options, timeoutMs) {
    options = options || {};
    timeoutMs = timeoutMs || 90000;
    var ctrl = new AbortController();
    var timer = setTimeout(function () {
      ctrl.abort();
    }, timeoutMs);
    var opts = Object.assign({}, options, { signal: ctrl.signal });
    return fetch(url, opts).finally(function () {
      clearTimeout(timer);
    });
  }

  function selectedChainId() {
    return CHAIN_TO_ID[selectedChain] || "0x2105";
  }

  function scoreClass(score) {
    if (score >= 65) return "hot";
    if (score > 0) return "warm";
    return "cold";
  }

  function hudScoreHtml(score) {
    return (
      '<span class="hud-score ' +
      scoreClass(score) +
      '">' +
      score +
      "</span>"
    );
  }

  function hudChainHtml(chain) {
    return '<span class="hud-chain">' + esc(CHAIN_LABEL[chain] || chain) + "</span>";
  }

  function renderChainOps(chainOps) {
    var el = document.getElementById("chainOps");
    if (!el) return;
    if (!chainOps || !chainOps.length) {
      el.innerHTML = '<span class="loading-hint">Ağ verisi yükleniyor…</span>';
      return;
    }
    var allowed = entryChains && entryChains.length ? entryChains : null;
    var filtered = allowed
      ? chainOps.filter(function (c) {
          return allowed.indexOf(c.chain) >= 0;
        })
      : chainOps;
    if (!filtered.length) {
      el.innerHTML = '<span class="loading-hint">Solana taraması bekleniyor…</span>';
      return;
    }
    el.innerHTML = filtered
      .map(function (c) {
        var isSol = c.chain === "solana";
        var active = c.chain === selectedChain ? " active" : "";
        var solClass = isSol ? " solana" : "";
        var top = c.top_pair ? esc(c.top_pair) : "—";
        return (
          '<button type="button" class="chain-pill' +
          active +
          solClass +
          '" data-chain="' +
          esc(c.chain) +
          '" title="' +
          (c.count ? c.count + " fırsat" : "Fırsat yok") +
          '">' +
          '<span class="c-name">' +
          (CHAIN_LABEL[c.chain] || c.chain) +
          "</span>" +
          '<span class="c-score ' +
          scoreClass(c.best_score) +
          '">' +
          (c.best_score || "—") +
          "</span>" +
          '<span class="c-top">' +
          top +
          "</span></button>"
        );
      })
      .join("");
    el.querySelectorAll(".chain-pill").forEach(function (pill) {
      pill.addEventListener("click", function () {
        selectChain(pill.getAttribute("data-chain"));
      });
    });
  }

  async function selectChain(chain) {
    selectedChain = chain;
    localStorage.setItem("selectedChain", chain);
    document.querySelectorAll(".chain-pill").forEach(function (p) {
      p.classList.toggle("active", p.getAttribute("data-chain") === chain);
    });
    if (phantomAddress && chain !== "solana") {
      /* EVM seçiliyken Phantom bakiyesi gizlenir */
    }
    updateWalletUI();
    refresh();
  }

  function shortAddr(a) {
    return a ? a.slice(0, 6) + "…" + a.slice(-4) : "";
  }

  function fmtBal(n) {
    if (n >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
    if (n >= 1) return String(Number(n.toFixed(4)));
    return String(Number(n.toFixed(6)));
  }

  function renderSolHoldings(data) {
    var body = document.getElementById("holdingsBody");
    var box = document.getElementById("walletHoldings");
    if (!body || !box) return;
    if (!data) {
      body.innerHTML = '<tr class="empty-row"><td colspan="3">Veri yok</td></tr>';
      return;
    }
    var rows = [
      '<tr class="chain-active"><td>Solana</td><td>SOL</td><td>' +
        fmtBal(data.sol || 0) +
        (data.tradeable_sol != null
          ? ' <span class="muted">(işlem: ' + fmtBal(data.tradeable_sol) + ")</span>"
          : "") +
        "</td></tr>",
    ];
    if (data.sol_price_usd) {
      rows.push(
        '<tr class="chain-active"><td></td><td>SOL/USD</td><td>$' +
          Number(data.sol_price_usd).toFixed(2) +
          "</td></tr>"
      );
    }
    if (data.deployable_usd != null) {
      rows.push(
        '<tr class="chain-active"><td></td><td>Alım gücü</td><td>$' +
          Number(data.deployable_usd).toFixed(2) +
          " (SOL)</td></tr>"
      );
    }
    body.innerHTML = rows.join("");
    box.hidden = false;
  }

  function renderHoldings(data) {
    var body = document.getElementById("holdingsBody");
    var box = document.getElementById("walletHoldings");
    if (!body || !box) return;
    if (!data || !data.chains) {
      body.innerHTML = '<tr class="empty-row"><td colspan="3">Veri yok</td></tr>';
      return;
    }
    var rows = [];
    ["base", "arbitrum", "bsc"].forEach(function (chain) {
      var c = data.chains[chain];
      if (!c) return;
      var active = chain === selectedChain ? " chain-active" : "";
      if (c.error) {
        rows.push(
          '<tr class="empty-row' +
            active +
            '"><td>' +
            (CHAIN_LABEL[chain] || chain) +
            '</td><td colspan="2">' +
            esc(c.error) +
            "</td></tr>"
        );
        return;
      }
      if (!c.tokens || !c.tokens.length) {
        rows.push(
          '<tr class="empty-row' +
            active +
            '"><td>' +
            (CHAIN_LABEL[chain] || chain) +
            '</td><td colspan="2">Bakiye yok</td></tr>'
        );
        return;
      }
      c.tokens.forEach(function (tok, i) {
        rows.push(
          '<tr class="' +
            active +
            '"><td>' +
            (i === 0 ? CHAIN_LABEL[chain] || chain : "") +
            "</td><td>" +
            esc(tok.symbol) +
            "</td><td>" +
            fmtBal(tok.balance) +
            "</td></tr>"
        );
      });
    });
    body.innerHTML = rows.length
      ? rows.join("")
      : '<tr class="empty-row"><td colspan="3">Bakiye yok</td></tr>';
    box.hidden = false;
  }

  async function refreshSolPortfolio() {
    var box = document.getElementById("walletHoldings");
    var body = document.getElementById("holdingsBody");
    if (!phantomAddress || !box || !body) return;
    box.hidden = false;
    body.innerHTML = '<tr class="empty-row"><td colspan="3">Yükleniyor…</td></tr>';
    try {
      var resp = await fetch(
        "/api/wallet/sol/portfolio?address=" + encodeURIComponent(phantomAddress)
      );
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      renderSolHoldings(await resp.json());
    } catch (_) {
      body.innerHTML =
        '<tr class="empty-row"><td colspan="3">Bakiye alınamadı — RPC hatası</td></tr>';
    }
  }

  async function syncPhantomBackend() {
    if (!phantomAddress) return null;
    try {
      var resp = await fetch("/api/wallet/sol/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pubkey: phantomAddress }),
      });
      var data = null;
      try {
        data = await resp.json();
      } catch (_) {}
      if (!resp.ok) {
        var detail = (data && data.detail) || "HTTP " + resp.status;
        return { ok: false, error: detail };
      }
      return data;
    } catch (e) {
      console.warn("phantom sync", e);
      return { ok: false, error: e.message || String(e) };
    }
  }

  async function refreshPortfolio() {
    await refreshSolPortfolio();
  }


  function esc(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function getPhantomProvider() {
    var p = window.phantom && window.phantom.solana;
    if (p && p.isPhantom) return p;
    if (window.solana && window.solana.isPhantom) return window.solana;
    return null;
  }

  function waitForPhantom(ms) {
    ms = ms || 4000;
    return new Promise(function (resolve) {
      var start = Date.now();
      (function tick() {
        var p = getPhantomProvider();
        if (p) return resolve(p);
        if (Date.now() - start >= ms) return resolve(null);
        setTimeout(tick, 200);
      })();
    });
  }

  function phantomErrorMsg(e) {
    if (!e) return "Bilinmeyen hata";
    if (e.code === 4001) return "Bağlantı reddedildi (Phantom popup iptal)";
    return e.message || String(e);
  }

  function showNoPhantom() {
    document.getElementById("walletStatus").innerHTML =
      '<span class="wallet-missing">Phantom bulunamadı. ' +
      '<a href="https://phantom.app/download" target="_blank" rel="noopener">Kur</a> ' +
      "→ Chrome/Brave ile <strong>http://127.0.0.1:8643</strong> aç.</span>";
    var btn = document.getElementById("phantomBtn");
    if (btn) {
      btn.disabled = false;
      btn.style.opacity = "1";
    }
  }

  async function updateWalletUI() {
    var btn = document.getElementById("phantomBtn");
    var status = document.getElementById("walletStatus");
    var holdings = document.getElementById("walletHoldings");
    if (!btn) return;
    btn.disabled = false;
    if (!phantomAddress) {
      btn.className = "qc-btn-wallet";
      btn.textContent = "Phantom";
      btn.title = "Phantom cüzdan bağlan";
      status.textContent = "Phantom bağlı değil — otomatik alım/satım için bağlan";
      if (holdings) holdings.hidden = true;
      return;
    }
    btn.className = "qc-btn-wallet connected";
    btn.textContent = shortAddr(phantomAddress);
    btn.title = "Bağlantıyı kes";
    status.innerHTML =
      'Phantom: <span class="addr">' +
      shortAddr(phantomAddress) +
      "</span> · Solana · bakiye senkron…";
    var synced = await syncPhantomBackend();
    await refreshPortfolio();
    var balNote = "";
    var syncNote = "";
    if (synced && synced.ok !== false && synced.balance_synced_usd != null) {
      balNote = " · motor $" + Number(synced.balance_synced_usd).toFixed(2);
    } else if (synced && synced.error) {
      syncNote =
        ' · <span class="wallet-missing">senkron hatası: ' + esc(synced.error) + "</span>";
    } else if (synced === null) {
      syncNote = ' · <span class="wallet-missing">motor kapalı — run komutu ile başlat</span>';
    }
    status.innerHTML =
      'Phantom: <span class="addr">' +
      shortAddr(phantomAddress) +
      "</span> · Solana" +
      balNote +
      syncNote +
      " · otomatik alım/satım aktif";
  }

  async function connectPhantom() {
    if (phantomAddress) {
      phantomAddress = "";
      localStorage.removeItem("phantomAddress");
      try {
        await fetch("/api/wallet/sol/connect", { method: "DELETE" });
      } catch (_) {}
      updateWalletUI();
      refresh();
      return;
    }
    var btn = document.getElementById("phantomBtn");
    var status = document.getElementById("walletStatus");
    btn.disabled = true;
    status.textContent = "Phantom aranıyor…";
    var provider = getPhantomProvider();
    if (!provider) {
      showNoPhantom();
      alert(
        "Phantom bulunamadı.\n\n" +
          "1) Chrome/Brave kullan\n" +
          "2) Phantom eklentisi kurulu olsun\n" +
          "3) http://127.0.0.1:8643 adresini aç"
      );
      btn.disabled = false;
      return;
    }
    status.textContent = "Phantom onayı bekleniyor…";
    try {
      var resp = await provider.connect({ onlyIfTrusted: false });
      var pk = resp.publicKey ? resp.publicKey.toString() : "";
      if (!pk && provider.publicKey) pk = provider.publicKey.toString();
      if (!pk) throw new Error("Hesap seçilmedi");
      phantomAddress = pk;
      localStorage.setItem("phantomAddress", phantomAddress);
      selectedChain = "solana";
      localStorage.setItem("selectedChain", "solana");
      await updateWalletUI();
      refresh();
    } catch (e) {
      status.textContent = phantomErrorMsg(e);
    } finally {
      btn.disabled = false;
    }
  }

  async function processPhantomPending() {
    if (phantomBusy || !phantomAddress) return;
    var provider = getPhantomProvider();
    if (!provider || !window.solanaWeb3) return;
    try {
      var resp = await fetch("/api/phantom/pending");
      if (!resp.ok) return;
      var data = await resp.json();
      var list = data.pending || [];
      if (!list.length) return;
      phantomBusy = true;
      var trade = list[0];
      var raw = Uint8Array.from(atob(trade.tx_base64), function (c) {
        return c.charCodeAt(0);
      });
      var tx = window.solanaWeb3.VersionedTransaction.deserialize(raw);
      var signed = await provider.signAndSendTransaction(tx);
      var sig =
        signed && signed.signature
          ? typeof signed.signature === "string"
            ? signed.signature
            : window.solanaWeb3.bs58.encode(signed.signature)
          : String(signed);
      await fetch("/api/phantom/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trade_id: trade.id, signature: sig }),
      });
      refresh();
    } catch (e) {
      console.warn("phantom pending", e);
    } finally {
      phantomBusy = false;
    }
  }

  async function initPhantom() {
    var status = document.getElementById("walletStatus");
    var provider = await waitForPhantom(4000);
    if (!provider) {
      if (phantomAddress) {
        status.textContent =
          "Phantom eklentisi bekleniyor — adres kayıtlı, senkron deneniyor…";
        await updateWalletUI();
      } else {
        status.textContent = "Phantom hazır değil — eklenti kur, sonra Phantom'a tıkla";
      }
      return;
    }
    try {
      if (provider.publicKey) {
        phantomAddress = provider.publicKey.toString();
        localStorage.setItem("phantomAddress", phantomAddress);
      } else if (phantomAddress) {
        try {
          await provider.connect({ onlyIfTrusted: true });
          if (provider.publicKey) {
            phantomAddress = provider.publicKey.toString();
            localStorage.setItem("phantomAddress", phantomAddress);
          }
        } catch (_) {
          /* Oturum süresi dolmuş — localStorage adresini koru; paper senkronu POST ile çalışır */
        }
      }
    } catch (_) {
      /* Adresi silme — yenilemede bağlantı kopmasın */
    }
    provider.on("accountChanged", function (pubkey) {
      if (pubkey) {
        phantomAddress = pubkey.toString();
        localStorage.setItem("phantomAddress", phantomAddress);
      } else {
        phantomAddress = "";
        localStorage.removeItem("phantomAddress");
      }
      updateWalletUI();
      refresh();
    });
    provider.on("disconnect", function () {
      /* Kullanıcı panelden kesene kadar adresi tut */
      updateWalletUI();
    });
    selectedChain = "solana";
    localStorage.setItem("selectedChain", "solana");
    if (phantomAddress) {
      await updateWalletUI();
    } else {
      status.textContent = "Phantom bağlı değil — cüzdan bakiyesinden otomatik işlem için bağlan";
    }
  }

  async function manualPosition(pool, action) {
    var url =
      action === "close"
        ? "/api/positions/" + encodeURIComponent(pool) + "/close"
        : "/api/positions/" + encodeURIComponent(pool) + "/partial?fraction=0.5";
    try {
      var r = await fetch(url, { method: "POST" });
      if (!r.ok) {
        var err = await r.json().catch(function () {
          return { detail: r.statusText };
        });
        throw new Error(err.detail || r.statusText);
      }
      await refresh();
    } catch (e) {
      alert("Manuel işlem başarısız: " + (e.message || e));
    }
  }

  async function refresh() {
    var data = await Promise.all([
      fetch("/api/state").then(function (r) {
        return r.json();
      }),
      fetch("/api/trades").then(function (r) {
        return r.json();
      }),
    ]);
    var state = data[0];
    var trades = data[1];
    if (state.decision && state.decision.entry_chains && state.decision.entry_chains.length) {
      entryChains = state.decision.entry_chains;
      selectedChain = "solana";
      localStorage.setItem("selectedChain", "solana");
    }
    var modeBadge = document.getElementById("modeBadge");
    if (modeBadge) {
      var label = state.mode.toUpperCase();
      if (state.mode === "paper" && state.live_sim && state.live_sim.enabled) {
        label = "PAPER · CANLI FİYAT";
      }
      modeBadge.textContent = label;
      modeBadge.className =
        "mode-badge" + (state.mode === "live" ? " live" : " paper-live");
    }
    document.getElementById("subtitle").textContent =
      "30 sn'de bir yenilenir" +
      (state.kill_switch ? " · Kill-switch aktif" : "") +
      (state.phantom && state.phantom.connected ? " · Phantom bağlı" : "") +
      (state.live_sim && state.live_sim.trade_execution === "paper"
        ? " · blockchain işlemi yok"
        : "") +
      (state.summary.live_chains
        ? " · Canlı ağlar: " + state.summary.live_chains.join(", ")
        : "");
    renderDecision(state.decision);
    renderTrendStack(state);
    renderEntryDiagnostics(state.entry_diagnostics);
    renderGrowthPotential(state.growth_potential);
    renderMarketIntel(state.market_intel);
    renderLiveSim(state.live_sim);
    renderHud(state);
    updatePathFan(state.watchlist);
    updateFormulaBar(state);
    renderPbTape(state.positions, trades, state.kill_switch);
    maybeAutoBrain(state.brain);
    document.getElementById("balance").textContent =
      state.balance != null
        ? "$" + state.balance.toFixed(2)
        : mmAddress
          ? "Cüzdan bağlı"
          : "—";
    var pnl = state.summary.session_pnl != null ? state.summary.session_pnl : state.summary.realized_pnl;
    var pnlEl = document.getElementById("pnl");
    pnlEl.textContent = (pnl >= 0 ? "+" : "") + "$" + pnl.toFixed(2);
    pnlEl.className = "value " + (pnl >= 0 ? "pos" : "neg");
    document.getElementById("open").textContent = state.summary.open_positions;
    document.getElementById("winrate").textContent = state.summary.win_rate + "%";
    renderChainOps(state.chain_opportunities);
    if (!selectedChain && state.chain_opportunities && state.chain_opportunities.length) {
      selectedChain = state.chain_opportunities[0].chain;
      localStorage.setItem("selectedChain", selectedChain);
    }
    var filteredWatch = state.watchlist.filter(function (w) {
      return !selectedChain || w.chain === selectedChain;
    });
    var watchTitle = document.getElementById("watchlistTitle");
    if (watchTitle) {
      watchTitle.textContent =
        "DEX Trending — " + (CHAIN_LABEL[selectedChain] || selectedChain);
    }
    document.getElementById("positions").innerHTML = state.positions.length
      ? state.positions
          .map(function (p) {
            var pnl = p.exit_quote_pnl != null ? p.exit_quote_pnl : p.unrealized_pnl;
            var exitCell =
              p.exit_quote_usd != null
                ? "$" +
                  p.exit_quote_usd.toFixed(2) +
                  (p.price_impact_pct != null
                    ? ' <span class="muted">(' + p.price_impact_pct + "%)</span>"
                    : "")
                : "—";
            var rowCls =
              "hud-row-new " + (pnl >= 0 ? "hud-row-win" : "hud-row-loss");
            return (
              '<tr class="' +
              rowCls +
              '"><td title="' +
              esc(p.pair) +
              '"><strong>' +
              esc(p.pair) +
              "</strong></td><td>" +
              hudChainHtml(p.chain) +
              "</td><td>" +
              p.entry_price +
              "</td><td>" +
              p.current_price +
              "</td><td>" +
              exitCell +
              "</td><td>$" +
              p.cost_usd +
              '</td><td class="hud-pnl-tag ' +
              (pnl >= 0 ? "pos" : "neg") +
              '">' +
              (pnl >= 0 ? "+" : "") +
              "$" +
              Number(pnl).toFixed(2) +
              "</td><td>" +
              hudScoreHtml(p.entry_score) +
              '</td><td class="qc-pos-actions">' +
              '<button type="button" class="qc-pos-btn qc-pos-btn-half" data-pool="' +
              esc(p.pool_address) +
              '" data-act="half" title="Yarısını sat">%50</button> ' +
              '<button type="button" class="qc-pos-btn qc-pos-btn-close" data-pool="' +
              esc(p.pool_address) +
              '" data-act="close" title="Tamamen kapat">KAPAT</button>' +
              "</td></tr>"
            );
          })
          .join("")
      : '<tr class="empty-row"><td colspan="9">Açık pozisyon yok</td></tr>';
    document.getElementById("watchlist").innerHTML = filteredWatch.length
      ? filteredWatch
          .map(function (w, i) {
            var boost =
              w.boost_score > 0
                ? ' <span class="dex-boost" title="Dexscreener boost">⚡' +
                  w.boost_score +
                  "</span>"
                : "";
            return (
              '<tr class="hud-row-new"><td class="dex-rank">' +
              (i + 1) +
              '</td><td class="dex-token" title="' +
              esc(w.name) +
              '"><strong>' +
              esc(w.name) +
              "</strong>" +
              boost +
              "</td><td>" +
              fmtMcap(w.market_cap_usd) +
              "</td><td>" +
              fmtAgeHours(w.age_hours) +
              "</td><td>" +
              fmtVolUsd(w.vol_h24) +
              "</td><td>" +
              (w.txns_h24 || "—") +
              "</td><td>" +
              (w.wallet_count != null ? w.wallet_count : "—") +
              (w.wallet_on_chain
                ? ' <span class="dex-boost" title="Helius on-chain">⛓</span>'
                : w.wallet_source === "proxy"
                  ? ' <span class="muted" title="Txns/hacim proxy">~</span>'
                  : "") +
              (w.whale_signal ? ' <span class="dex-boost" title="Balina">🐋</span>' : "") +
              "</td><td>" +
              (w.turnover != null ? w.turnover.toFixed(0) + "x" : "—") +
              "</td>" +
              chgPctCell(w.chg_m5) +
              chgPctCell(w.chg_h1) +
              chgPctCell(w.chg_h24) +
              "<td>" +
              fmtVolUsd(w.liquidity_usd) +
              "</td><td>" +
              (w.moon_tag || (w.moonshot_score >= 62 ? "🎯" : "—")) +
              (w.moonshot_score != null && w.moonshot_score >= 48
                ? ' <span class="muted" title="' +
                  esc((w.signals || []).join(", ")) +
                  '">' +
                  w.moonshot_score +
                  "</span>"
                : "") +
              "</td><td>" +
              hudScoreHtml(w.score) +
              "</td></tr>"
            );
          })
          .join("")
      : '<tr class="empty-row"><td colspan="14">Trend aday yok</td></tr>';
    document.getElementById("trades").innerHTML = trades.length
      ? trades
          .slice(0, 12)
          .map(function (t) {
            var win = t.pnl_usd >= 0;
            var type = t.exit_reason && t.exit_reason.indexOf("tp") >= 0 ? "PARTIAL" : "SELL";
            return (
              '<tr class="hud-row-new ' +
              (win ? "hud-row-win" : "hud-row-loss") +
              '"><td title="' +
              esc(t.pair_name) +
              '">' +
              esc(t.pair_name) +
              "</td><td class=\"" +
              (win ? "pos" : "neg") +
              '">' +
              type +
              '</td><td class="text-right hud-pnl-tag ' +
              (win ? "pos" : "neg") +
              '">' +
              (win ? "+" : "") +
              "$" +
              t.pnl_usd.toFixed(2) +
              '</td><td class="text-right hud-reason">' +
              esc(t.exit_reason) +
              "</td></tr>"
            );
          })
          .join("")
      : '<tr class="empty-row"><td colspan="4">Henüz işlem yok</td></tr>';
    if (mmAddress) refreshPortfolio();
  }

  function getTheme() {
    return document.documentElement.getAttribute("data-theme") || "dark";
  }

  function applyThemeIcon(theme) {
    var sun = document.getElementById("themeIconSun");
    var moon = document.getElementById("themeIconMoon");
    if (!sun || !moon) return;
    if (theme === "light") {
      sun.style.display = "none";
      moon.style.display = "block";
    } else {
      sun.style.display = "block";
      moon.style.display = "none";
    }
  }

  function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("theme", theme);
    applyThemeIcon(theme);
  }

  function toggleTheme() {
    setTheme(getTheme() === "dark" ? "light" : "dark");
  }

  function initTheme() {
    var saved = localStorage.getItem("theme");
    var theme = saved === "light" || saved === "dark" ? saved : "dark";
    setTheme(theme);
  }

  var scanModeIds = ["cex", "news", "whale", "derivatives", "grid"];

  var CHAIN_HUD_COLOR = {
    solana: "#9945FF",
    base: "#0052FF",
    arbitrum: "#28A0F0",
    bsc: "#F3BA2F",
  };
  var equityHistory = [];
  var hudPrevFeedKey = "";
  var pathFanPaths = [];
  var pathFanRaf = null;
  var pathFanT = 0;
  var pathFanWatchKey = "";

  function streamTime() {
    var d = new Date();
    return (
      String(d.getHours()).padStart(2, "0") +
      ":" +
      String(d.getMinutes()).padStart(2, "0") +
      ":" +
      String(d.getSeconds()).padStart(2, "0")
    );
  }

  function buildPathFanPaths(watchlist) {
    var items = (watchlist || []).slice(0, 28);
    if (!items.length) {
      items = new Array(18).fill(null).map(function (_, i) {
        return { chg_h1: (i - 9) * 2.2, name: "scan" };
      });
    }
    return items.map(function (w, i) {
      var chg = w.chg_h1 != null ? Number(w.chg_h1) : 0;
      var spread = items.length > 1 ? i / (items.length - 1) : 0.5;
      return {
        spread: spread * 2 - 1,
        chg: chg,
        label: (chg >= 0 ? "+" : "") + chg.toFixed(1) + "%",
        pos: chg >= 0,
        wobble: 0.25 + (i % 7) * 0.11,
        phase: i * 0.37,
        alpha: 0.12 + (Math.abs(chg) / 120) * 0.35,
      };
    });
  }

  function resizePathFanCanvas(canvas) {
    if (!canvas) return null;
    var rect = canvas.getBoundingClientRect();
    var w = Math.max(320, Math.floor(rect.width) || canvas.width);
    var h = Math.max(140, Math.floor(rect.height) || 220);
    var dpr = window.devicePixelRatio || 1;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    var ctx = canvas.getContext("2d");
    if (ctx) ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx: ctx, w: w, h: h };
  }

  function drawPathFanFrame() {
    var canvas = document.getElementById("pbPathFan");
    if (!canvas || !pathFanPaths.length) return;
    var sized = resizePathFanCanvas(canvas);
    if (!sized || !sized.ctx) return;
    var ctx = sized.ctx;
    var w = sized.w;
    var h = sized.h;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, w, h);

    var ox = w * 0.08;
    var oy = h * 0.52;
    var maxSpread = h * 0.42;

    ctx.beginPath();
    ctx.arc(ox, oy, 4, 0, Math.PI * 2);
    ctx.fillStyle = "#fff";
    ctx.fill();
    ctx.shadowColor = "rgba(255,255,255,0.8)";
    ctx.shadowBlur = 12;

    pathFanPaths.forEach(function (p) {
      var endX = w * (0.82 + Math.sin(pathFanT * 0.4 + p.phase) * 0.04);
      var endY = oy + p.spread * maxSpread * (0.65 + Math.sin(pathFanT + p.phase) * 0.08);
      var cp1x = ox + (endX - ox) * 0.35;
      var cp1y = oy + Math.sin(pathFanT * p.wobble + p.phase) * 18;
      var cp2x = ox + (endX - ox) * 0.72;
      var cp2y = endY + Math.cos(pathFanT * 0.6 + p.phase) * 12;

      ctx.beginPath();
      ctx.moveTo(ox, oy);
      ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, endX, endY);
      ctx.strokeStyle = p.pos
        ? "rgba(255,255,255," + Math.min(0.55, p.alpha + 0.15) + ")"
        : "rgba(255,255,255," + Math.min(0.4, p.alpha) + ")";
      ctx.lineWidth = 0.6 + Math.abs(p.chg) * 0.015;
      ctx.stroke();

      if (Math.abs(p.chg) >= 4) {
        ctx.beginPath();
        ctx.arc(endX, endY, 2.2, 0, Math.PI * 2);
        ctx.fillStyle = p.pos ? "#4ade80" : "#f87171";
        ctx.fill();
        ctx.font = "9px " + (getComputedStyle(document.body).fontFamily || "monospace");
        ctx.fillStyle = p.pos ? "rgba(74,222,128,0.85)" : "rgba(248,113,113,0.85)";
        ctx.fillText(p.label, endX + 4, endY + 3);
      }
    });
    ctx.shadowBlur = 0;
    pathFanT += 0.018;
  }

  function startPathFanLoop() {
    if (pathFanRaf) return;
    function tick() {
      drawPathFanFrame();
      pathFanRaf = requestAnimationFrame(tick);
    }
    pathFanRaf = requestAnimationFrame(tick);
  }

  function updatePathFan(watchlist) {
    var key = (watchlist || [])
      .map(function (w) {
        return w.name + (w.chg_h1 || 0);
      })
      .join("|");
    if (key !== pathFanWatchKey) {
      pathFanWatchKey = key;
      pathFanPaths = buildPathFanPaths(watchlist);
    }
    var label = document.getElementById("pbPathLabel");
    if (label) {
      label.textContent =
        watchlist && watchlist.length
          ? watchlist.length + " paths · DEX momentum fan"
          : "scan → entry paths";
    }
  }

  function updateFormulaBar(state) {
    var dec = state.decision || {};
    var ep = dec.exit_policy || {};
    var bayes = document.getElementById("pbFormulaBayes");
    var edge = document.getElementById("pbFormulaEdge");
    var exec = document.getElementById("pbFormulaExec");
    if (bayes) {
      bayes.textContent =
        "post " +
        (dec.macro_avg != null ? dec.macro_avg : "—") +
        " · pen " +
        (dec.brain_penalty || 0);
    }
    if (edge) {
      edge.textContent =
        "konf ≥ " +
        (dec.confluence_min != null ? dec.confluence_min : "—") +
        " · " +
        (dec.aggressive ? "AGGR ON" : "normal");
    }
    if (exec) {
      exec.textContent =
        ep.runner_trail_pct != null
          ? "trail " + ep.runner_trail_pct + "% · peak intel"
          : "peak trail · dynamic exit";
    }
  }

  function animateHudNumber(el, nextText, cssClass) {
    if (!el) return;
    if (el.textContent === nextText && (!cssClass || el.classList.contains(cssClass))) return;
    el.classList.remove("pos", "neg", "is-tick");
    if (cssClass) el.classList.add(cssClass);
    el.classList.add("is-tick");
    setTimeout(function () {
      el.textContent = nextText;
      el.classList.remove("is-tick");
    }, 90);
  }

  function totalEquity(state) {
    var bal = state.balance != null ? state.balance : 0;
    var posVal = 0;
    (state.positions || []).forEach(function (p) {
      var pnl = p.exit_quote_pnl != null ? p.exit_quote_pnl : p.unrealized_pnl || 0;
      posVal += (p.cost_usd || 0) + pnl;
    });
    return bal + posVal;
  }

  function drawTerminalChart(canvasId, equity) {
    var canvas = document.getElementById(canvasId);
    if (!canvas) return;
    var ctx = canvas.getContext("2d");
    if (!ctx) return;
    equityHistory.push(equity);
    if (equityHistory.length > 64) equityHistory.shift();
    var w = canvas.width;
    var h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    if (equityHistory.length < 2) return;
    var min = Math.min.apply(null, equityHistory);
    var max = Math.max.apply(null, equityHistory);
    var padX = 8;
    var padY = 10;
    var range = max - min || 1;
    var highEl = document.getElementById("pbChartHigh");
    var lowEl = document.getElementById("pbChartLow");
    if (highEl) highEl.textContent = "H $" + max.toFixed(2);
    if (lowEl) lowEl.textContent = "L $" + min.toFixed(2);

    ctx.strokeStyle = "rgba(34, 211, 238, 0.08)";
    ctx.lineWidth = 1;
    for (var g = 0; g < 5; g++) {
      var gy = padY + ((h - padY * 2) * g) / 4;
      ctx.beginPath();
      ctx.moveTo(padX, gy);
      ctx.lineTo(w - padX, gy);
      ctx.stroke();
    }

    var pts = [];
    equityHistory.forEach(function (v, i) {
      var x = padX + (i / (equityHistory.length - 1)) * (w - padX * 2);
      var y = h - padY - ((v - min) / range) * (h - padY * 2);
      pts.push({ x: x, y: y });
    });

    ctx.beginPath();
    ctx.moveTo(pts[0].x, h - padY);
    pts.forEach(function (p) {
      ctx.lineTo(p.x, p.y);
    });
    ctx.lineTo(pts[pts.length - 1].x, h - padY);
    ctx.closePath();
    var grad = ctx.createLinearGradient(0, padY, 0, h);
    grad.addColorStop(0, "rgba(34, 211, 238, 0.28)");
    grad.addColorStop(1, "rgba(34, 211, 238, 0)");
    ctx.fillStyle = grad;
    ctx.fill();

    ctx.beginPath();
    ctx.strokeStyle = "#22d3ee";
    ctx.lineWidth = 2;
    ctx.shadowColor = "rgba(34, 211, 238, 0.55)";
    ctx.shadowBlur = 10;
    pts.forEach(function (p, i) {
      if (i === 0) ctx.moveTo(p.x, p.y);
      else ctx.lineTo(p.x, p.y);
    });
    ctx.stroke();
    ctx.shadowBlur = 0;

    var last = pts[pts.length - 1];
    ctx.fillStyle = "#4ade80";
    ctx.beginPath();
    ctx.arc(last.x, last.y, 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "rgba(74, 222, 128, 0.5)";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(last.x, last.y, 7, 0, Math.PI * 2);
    ctx.stroke();
  }

  function drawHudSparkline(equity) {
    drawTerminalChart("pbEquityChart", equity);
  }

  function tokenShort(name) {
    if (!name) return "—";
    var parts = name.split("/");
    return parts[0].trim().slice(0, 8);
  }

  function renderPbDepth(watchlist) {
    var el = document.getElementById("pbDepthBars");
    if (!el) return;
    if (!watchlist || !watchlist.length) {
      el.innerHTML = '<div class="pb-depth-row"><span class="pb-depth-label">—</span></div>';
      return;
    }
    var top = watchlist.slice(0, 7);
    var maxVol = 1;
    top.forEach(function (w) {
      maxVol = Math.max(maxVol, w.vol_h24 || w.liquidity_usd || 1);
    });
    el.innerHTML = top
      .map(function (w) {
        var vol = w.vol_h24 || w.liquidity_usd || 0;
        var pct = Math.max(4, (vol / maxVol) * 100);
        var chg = w.chg_h1 != null ? w.chg_h1 : 0;
        var cls = chg >= 0 ? "pos" : "neg";
        return (
          '<div class="pb-depth-row">' +
          '<span class="pb-depth-label">' +
          esc(tokenShort(w.name)) +
          '</span><div class="pb-depth-track"><div class="pb-depth-fill ' +
          cls +
          '" style="width:' +
          pct.toFixed(1) +
          '%"></div></div><span class="pb-depth-chg ' +
          cls +
          '">' +
          (chg >= 0 ? "+" : "") +
          chg.toFixed(1) +
          "%</span></div>"
        );
      })
      .join("");
  }

  var pbTapePrevKey = "";

  function renderPbTape(positions, trades, killSwitch) {
    var list = document.getElementById("pbTape");
    var countEl = document.getElementById("pbTapeCount");
    if (!list) return;
    var rows = [];
    if (killSwitch) {
      rows.push({
        tag: "kill",
        tagLabel: "[KILL]",
        pair: "switch active",
        meta: "new entries halted",
        pnl: null,
      });
    }
    (positions || []).forEach(function (p) {
      var pnl = p.exit_quote_pnl != null ? p.exit_quote_pnl : p.unrealized_pnl || 0;
      rows.push({
        tag: "open",
        tagLabel: "[OPEN]",
        pair: p.pair,
        meta: p.chain + " · $" + Number(p.cost_usd || 0).toFixed(0),
        pnl: pnl,
      });
    });
    (trades || []).slice(0, 8).forEach(function (t) {
      var side = (t.type || t.side || "trade").toLowerCase();
      var tag = side.indexOf("sell") >= 0 || side.indexOf("exit") >= 0 ? "fill" : "scan";
      rows.push({
        tag: tag,
        tagLabel: tag === "fill" ? "[FILL]" : "[SCAN]",
        pair: t.pair || "—",
        meta: (t.reason || side).slice(0, 28),
        pnl: t.pnl_usd != null ? t.pnl_usd : null,
      });
    });
    var key = rows
      .map(function (r) {
        return r.pair + r.meta + r.pnl;
      })
      .join("|");
    if (key === pbTapePrevKey) return;
    pbTapePrevKey = key;
    if (countEl) countEl.textContent = rows.length + " events";
    if (!rows.length) {
      list.innerHTML =
        '<li><span class="pb-tape-pair">Bekleniyor…</span><span class="pb-tape-meta">scan</span></li>';
      return;
    }
    list.innerHTML = rows
      .slice(0, 10)
      .map(function (r) {
        var pnlHtml = "";
        if (r.pnl != null) {
          var cls = r.pnl >= 0 ? "pos" : "neg";
          pnlHtml =
            '<span class="pb-tape-pnl ' +
            cls +
            '">' +
            (r.pnl >= 0 ? "+" : "") +
            "$" +
            Number(r.pnl).toFixed(2) +
            "</span>";
        }
        return (
          "<li><div><div class=\"pb-tape-pair\"><span class=\"pb-tape-tag " +
          esc(r.tag) +
          '">' +
          esc(r.tagLabel) +
          "</span> " +
          esc(r.pair) +
          '</div><div class="pb-tape-meta">' +
          streamTime() +
          " · " +
          esc(r.meta) +
          "</div></div>" +
          pnlHtml +
          "</li>"
        );
      })
      .join("");
  }

  function renderHudDonut(positions) {
    var svg = document.getElementById("hudDonut");
    if (!svg) return;
    var byChain = {};
    var total = 0;
    (positions || []).forEach(function (p) {
      var c = p.chain || "?";
      byChain[c] = (byChain[c] || 0) + (p.cost_usd || 0);
      total += p.cost_usd || 0;
    });
    if (!total) {
      svg.innerHTML =
        '<circle cx="100" cy="100" r="70" fill="none" stroke="rgba(34,211,238,0.12)" stroke-width="22"/>';
      return;
    }
    var r = 70;
    var cx = 100;
    var cy = 100;
    var circ = 2 * Math.PI * r;
    var offset = 0;
    var parts = [];
    Object.keys(byChain).forEach(function (chain) {
      var frac = byChain[chain] / total;
      var dash = circ * frac;
      var color = CHAIN_HUD_COLOR[chain] || "#64748b";
      parts.push(
        '<circle cx="' +
          cx +
          '" cy="' +
          cy +
          '" r="' +
          r +
          '" fill="none" stroke="' +
          color +
          '" stroke-width="22" stroke-dasharray="' +
          dash +
          " " +
          (circ - dash) +
          '" stroke-dashoffset="' +
          -offset +
          '" transform="rotate(-90 ' +
          cx +
          " " +
          cy +
          ')"/>'
      );
      offset += dash;
    });
    svg.innerHTML = parts.join("");
  }

  function renderHudFeed(positions) {
    var feed = document.getElementById("hudFeed");
    if (!feed) return;
    var key = (positions || [])
      .map(function (p) {
        return p.pair + p.current_price + (p.exit_quote_pnl != null ? p.exit_quote_pnl : p.unrealized_pnl);
      })
      .join("|");
    if (key === hudPrevFeedKey) return;
    hudPrevFeedKey = key;
    if (!positions || !positions.length) {
      feed.innerHTML =
        '<li class="hud-feed-item"><div class="hud-feed-row"><span class="hud-feed-pair">Pozisyon yok</span></div><div class="hud-feed-meta"><span>Tarama bekleniyor</span></div></li>';
      return;
    }
    feed.innerHTML = positions
      .map(function (p) {
        var pnl = p.exit_quote_pnl != null ? p.exit_quote_pnl : p.unrealized_pnl;
        var pnlCls = pnl >= 0 ? "pos" : "neg";
        return (
          '<li class="hud-feed-item"><div class="hud-feed-row"><span class="hud-feed-pair">' +
          esc(p.pair) +
          '</span><span class="hud-feed-pnl ' +
          pnlCls +
          '">' +
          (pnl >= 0 ? "+" : "") +
          "$" +
          Number(pnl).toFixed(2) +
          '</span></div><div class="hud-feed-meta"><span>' +
          esc(p.chain) +
          " · $" +
          p.cost_usd +
          '</span><span>' +
          p.current_price +
          "</span></div></li>"
        );
      })
      .join("");
  }

  function renderObOrbit(state) {
    var netEl = document.getElementById("obNetwork");
    var trendEl = document.getElementById("obTrendStatus");
    var sim = state.live_sim || {};
    if (netEl) {
      var mode = (state.mode || "paper").toUpperCase();
      var feed = (sim.price_feed || "DEX").replace(/geckoterminal/i, "Gecko");
      netEl.textContent = mode + " · " + feed;
    }
    if (trendEl) {
      var diag = state.entry_diagnostics || {};
      var passing = diag.would_enter_count || 0;
      if (passing > 0) {
        trendEl.textContent = passing + " GİRİŞ ADAYI";
      } else if (state.decision && state.decision.aggressive) {
        trendEl.textContent = "AGGRESSIVE SCAN";
      } else {
        trendEl.textContent = "TREND TARANIYOR";
      }
    }
  }

  function renderTrendStack(state) {
    var st = document.getElementById("trendST");
    var ht = document.getElementById("trendHT");
    var ut = document.getElementById("trendUT");
    var status = document.getElementById("trendStatus");
    var modeEl = document.getElementById("trendEntryMode");
    var minEl = document.getElementById("trendConfluenceMin");
    if (!st || !status) return;

    var intel = state.market_intel || {};
    var holds = (intel.binance_holds || []).concat(intel.okx_holds || []);
    var best = holds.length ? holds[0] : null;
    var metrics = best && best.metrics ? best.metrics : {};
    var aboveEma = metrics.above_ema200;
    var volOk = (metrics.vol_spike || 0) >= 1.12;
    var macdUp = (metrics.macd_hist || 0) > 0;

    function setPill(el, on, label) {
      if (!el) return;
      el.textContent = on ? "AL" : "—";
      el.parentElement.classList.toggle("is-on", !!on);
      if (label && on) el.textContent = label;
    }

    setPill(st, metrics.supertrend_bull || (aboveEma && macdUp), "AL");
    setPill(ht, metrics.halftrend_bull || (metrics.rsi >= 38 && metrics.rsi <= 62 && macdUp), "AL");
    setPill(
      ut,
      metrics.ut_bot_alert || metrics.ut_bot_bull || metrics.ut_bot || (volOk && macdUp && (metrics.chg_24h_pct || 0) >= 2.5),
      metrics.ut_bot_alert ? "ALERT" : "BOOST",
    );

    var decision = state.decision || {};
    var diag = state.entry_diagnostics || {};
    if (modeEl) {
      modeEl.textContent = decision.aggressive ? "AGGRESSIVE · GİRİŞ AÇIK" : "NORMAL";
    }
    if (minEl && decision.confluence_min != null) {
      minEl.textContent = "konfluans ≥ " + decision.confluence_min;
    }

    var ep = decision.exit_policy || {};
    var ce = document.getElementById("trendCE");
    if (ce && ep.runner_arm_pct != null) {
      var ceLabel = metrics.chandelier_stop > 0 ? "CE " + metrics.chandelier_stop.toFixed(4) : "+" + ep.runner_arm_pct + "% TRAIL";
      ce.textContent = ceLabel;
    }

    var last = decision.last;
    if (last && last.action === "enter") {
      status.className = "trend-status hud-status-bar hud-active";
      status.textContent = "GİRİŞ: " + last.pair + " — " + (last.reason || "");
    } else if (last && (last.action === "exit" || last.action === "exit_partial")) {
      status.className = "trend-status hud-status-bar hud-warn";
      status.textContent = "ÇIKIŞ: " + last.pair + " — " + (last.reason || "");
    } else {
      status.className = "trend-status hud-status-bar hud-pending";
      status.textContent =
        (diag.would_enter_count || 0) +
        " aday konfluans geçti · trend+CEX+DEX filtresi aktif";
    }

    var visual = document.getElementById("saitoBrainVisual");
    if (visual) {
      visual.classList.toggle("is-ready", (diag.would_enter_count || 0) > 0);
    }
  }

  function positionTotals(state, unrealized) {
    var positions = state.positions || [];
    var openCount = state.summary.open_positions || positions.length || 0;
    var deployed =
      state.summary.deployed_usd != null
        ? Number(state.summary.deployed_usd)
        : 0;
    if (!deployed && positions.length) {
      positions.forEach(function (p) {
        deployed += Number(p.cost_usd || 0);
      });
    }
    var uPnl = unrealized;
    if (!uPnl && positions.length) {
      uPnl = 0;
      positions.forEach(function (p) {
        uPnl += Number(
          p.exit_quote_pnl != null ? p.exit_quote_pnl : p.unrealized_pnl || 0
        );
      });
    }
    var marketValue =
      state.summary.positions_value_usd != null
        ? Number(state.summary.positions_value_usd)
        : deployed + uPnl;
    return {
      openCount: openCount,
      deployed: deployed,
      marketValue: marketValue,
      uPnl: uPnl,
    };
  }

  function updatePositionsTotals(state, unrealized) {
    var t = positionTotals(state, unrealized);
    var fmtPnl = function (n) {
      return (n >= 0 ? "+" : "") + "$" + n.toFixed(2);
    };
    var costEl = document.getElementById("positionsTotalCost");
    var valueEl = document.getElementById("positionsTotalValue");
    var pnlEl = document.getElementById("positionsTotalPnl");
    var countEl = document.getElementById("positionsTotalCount");
    var footCost = document.getElementById("positionsFootCost");
    var footPnl = document.getElementById("positionsFootPnl");
    var foot = document.getElementById("positionsFoot");
    var bar = document.getElementById("positionsTotalBar");
    if (costEl) costEl.textContent = "$" + t.deployed.toFixed(2);
    if (valueEl) valueEl.textContent = "$" + t.marketValue.toFixed(2);
    if (pnlEl) {
      pnlEl.textContent = fmtPnl(t.uPnl);
      pnlEl.classList.remove("pos", "neg");
      if (t.uPnl > 0) pnlEl.classList.add("pos");
      else if (t.uPnl < 0) pnlEl.classList.add("neg");
    }
    if (countEl) countEl.textContent = String(t.openCount);
    if (footCost) footCost.innerHTML = "<strong>$" + t.deployed.toFixed(2) + "</strong>";
    if (footPnl) {
      footPnl.innerHTML =
        '<strong class="' +
        (t.uPnl >= 0 ? "pos" : "neg") +
        '">' +
        fmtPnl(t.uPnl) +
        "</strong>";
    }
    if (foot) foot.style.display = t.openCount > 0 ? "" : "none";
    if (bar) bar.classList.toggle("has-positions", t.openCount > 0);
  }

  function renderHud(state) {
    renderObOrbit(state);
    var equity = state.summary.equity != null ? state.summary.equity : totalEquity(state);
    var realized = state.summary.realized_pnl || 0;
    var sessionPnl =
      state.summary.session_pnl != null
        ? state.summary.session_pnl
        : realized + (state.summary.unrealized_pnl || 0);
    var sessionPct = state.summary.session_pnl_pct;
    var unrealized = state.summary.unrealized_pnl || 0;
    if (!state.summary.unrealized_pnl && state.positions && state.positions.length) {
      unrealized = 0;
      (state.positions || []).forEach(function (p) {
        unrealized += p.exit_quote_pnl != null ? p.exit_quote_pnl : p.unrealized_pnl || 0;
      });
    }

    animateHudNumber(
      document.getElementById("hudBalance"),
      state.balance != null ? "$" + state.balance.toFixed(2) : "—"
    );

    var sessionEl = document.getElementById("hudSessionPnl");
    if (sessionEl) {
      var sessionText =
        (sessionPnl >= 0 ? "+" : "") + "$" + sessionPnl.toFixed(2) + " oturum";
      animateHudNumber(sessionEl, sessionText, sessionPnl >= 0 ? "pos" : "neg");
    }
    var pctEl = document.getElementById("hudSessionPct");
    if (pctEl && sessionPct != null && state.summary.start_balance_usd) {
      pctEl.textContent =
        " · " +
        (sessionPct >= 0 ? "+" : "") +
        sessionPct.toFixed(2) +
        "% vs $" +
        Number(state.summary.start_balance_usd).toFixed(0);
      pctEl.className = "qc-delta-sub " + (sessionPct >= 0 ? "pos" : "neg");
    } else if (pctEl) {
      pctEl.textContent = "";
    }

    var realizedLine = document.getElementById("hudRealizedLine");
    if (realizedLine) {
      var gasNote =
        state.summary.gas_paid_est != null
          ? " · gas ~$" + state.summary.gas_paid_est.toFixed(2)
          : "";
      var gross =
        state.summary.gross_profit != null && state.summary.gross_loss != null
          ? " · brüt +" +
            state.summary.gross_profit.toFixed(2) +
            " / " +
            state.summary.gross_loss.toFixed(2)
          : "";
      realizedLine.textContent =
        "Gerçekleşen " +
        (realized >= 0 ? "+" : "") +
        "$" +
        realized.toFixed(2) +
        (unrealized ? " · açık " + (unrealized >= 0 ? "+" : "") + "$" + unrealized.toFixed(2) : "") +
        gross +
        gasNote;
    }

    var pnlLegacy = document.getElementById("hudPnl");
    if (pnlLegacy) {
      animateHudNumber(
        pnlLegacy,
        (sessionPnl >= 0 ? "+" : "") + "$" + sessionPnl.toFixed(2),
        sessionPnl >= 0 ? "pos" : "neg"
      );
    }
    var pnlOrb = document.getElementById("hudPnlOrb");
    if (pnlOrb) {
      pnlOrb.classList.remove("pos", "neg");
      if (sessionPnl > 0) pnlOrb.classList.add("pos");
      else if (sessionPnl < 0) pnlOrb.classList.add("neg");
    }
    var openCount = state.summary.open_positions || 0;
    var deployed =
      state.summary.deployed_usd != null
        ? Number(state.summary.deployed_usd)
        : 0;
    if (deployed === 0 && state.positions && state.positions.length) {
      deployed = 0;
      (state.positions || []).forEach(function (p) {
        deployed += Number(p.cost_usd || 0);
      });
    }
    var positionMv =
      state.summary.positions_value_usd != null
        ? Number(state.summary.positions_value_usd)
        : deployed + unrealized;
    animateHudNumber(
      document.getElementById("hudPositionUsd"),
      "$" + deployed.toFixed(2)
    );
    var positionMeta = document.getElementById("hudPositionMeta");
    if (positionMeta) {
      var meta =
        openCount +
        " açık · $" +
        positionMv.toFixed(2) +
        " değer";
      if (openCount > 0 && unrealized) {
        meta +=
          " · " +
          (unrealized >= 0 ? "+" : "") +
          "$" +
          unrealized.toFixed(2) +
          " uPnL";
      }
      positionMeta.textContent = meta;
    }
    updatePositionsTotals(state, unrealized);
    animateHudNumber(
      document.getElementById("hudWinrate"),
      state.summary.win_rate + "%"
    );
    var winBar = document.getElementById("hudWinBar");
    if (winBar) {
      var wr = Math.max(0, Math.min(100, state.summary.win_rate || 0));
      winBar.style.width = wr + "%";
    }
    var winBreak = document.getElementById("hudWinBreakdown");
    if (winBreak && state.summary.trade_count) {
      var wins = state.summary.wins != null ? state.summary.wins : "—";
      var losses = state.summary.losses != null ? state.summary.losses : "—";
      winBreak.textContent =
        wins + " kazanç · " + losses + " kayıp · " + state.summary.trade_count + " kapanış";
    } else if (winBreak) {
      winBreak.textContent = "Henüz kapanış yok";
    }

    var pnlDelta = document.getElementById("hudPnlDelta");
    if (pnlDelta) {
      pnlDelta.classList.remove("pos", "neg");
      if (sessionPnl > 0) pnlDelta.classList.add("pos");
      else if (sessionPnl < 0) pnlDelta.classList.add("neg");
    }

    var signalText = document.getElementById("hudSignalText");
    var signalDot = document.getElementById("hudSignalDot");
    if (signalText) {
      if (openCount > 0) {
        signalText.textContent = openCount + " pozisyon aktif";
      } else if (state.watchlist && state.watchlist.length) {
        signalText.textContent = state.watchlist.length + " aday izleniyor";
      } else {
        signalText.textContent = "Sinyal bekleniyor";
      }
    }
    if (signalDot) {
      signalDot.classList.toggle("live", (state.summary.open_positions || 0) > 0);
    }

    animateHudNumber(
      document.getElementById("hudEquity"),
      "$" + equity.toFixed(2)
    );
    var eqMetric = document.getElementById("hudEquityMetric");
    if (eqMetric) {
      animateHudNumber(eqMetric, "$" + equity.toFixed(2));
    }
    if (
      equityHistory.length === 0 &&
      state.summary &&
      state.summary.start_balance_usd != null
    ) {
      equityHistory.push(Number(state.summary.start_balance_usd));
    }

    var botEl = document.getElementById("hudBotStatus");
    if (botEl) {
      if (state.kill_switch) {
        botEl.textContent = "KILL";
        botEl.className = "pb-term-motor kill";
      } else {
        botEl.textContent = "ONLINE";
        botEl.className = "pb-term-motor";
      }
    }

    var apiSt = document.getElementById("pbApiStatus");
    if (apiSt && !state.kill_switch) {
      apiSt.innerHTML = '<span class="pb-pulse"></span> STREAMING';
    } else if (apiSt) {
      apiSt.innerHTML = '<span class="pb-pulse" style="background:#f87171"></span> HALTED';
    }

    var orbit = document.getElementById("hudOrbitMetrics");
    if (orbit) {
      orbit.innerHTML =
        '<div class="qc-hub-chip"><span>OTURUM</span>' +
        (sessionPnl >= 0 ? "+" : "") +
        sessionPnl.toFixed(2) +
        " USD</div>" +
        '<div class="qc-hub-chip"><span>BRÜT +</span>' +
        (state.summary.gross_profit != null
          ? state.summary.gross_profit.toFixed(2)
          : "—") +
        "</div>" +
        '<div class="qc-hub-chip"><span>POZİSYON</span>' +
        state.summary.open_positions +
        "</div>";
    }

    drawHudSparkline(equity);
    renderPbDepth(state.watchlist || []);
    renderHudDonut(state.positions);
    renderHudFeed(state.positions);
  }

  function renderLiveSim(sim) {
    var desc = document.getElementById("liveSimDesc");
    var tags = document.getElementById("liveSimTags");
    var hint = document.getElementById("positionsModeHint");
    if (!sim || !desc) return;
    desc.textContent =
      sim.trade_execution === "paper"
        ? "paper · canlı DEX fiyatı"
        : sim.trade_execution || "—";
    if (tags) {
      tags.innerHTML =
        '<span class="sim-tag">İşlem: ' +
        esc(sim.trade_execution || "paper") +
        '</span><span class="sim-tag">Fiyat: ' +
        esc(sim.price_feed || "gecko") +
        '</span><span class="sim-tag">Çıkış: ' +
        esc(sim.exit_quotes || "—") +
        "</span>";
    }
    if (hint) {
      hint.textContent =
        sim.trade_execution === "paper"
          ? "(sanal işlem · gerçek DEX fiyatı)"
          : "(canlı işlem)";
    }
  }

  function renderMarketIntel(intel) {
    if (!intel) return;
    var whaleBody = document.getElementById("whaleTable");
    var binBody = document.getElementById("binanceHolds");
    var okxBody = document.getElementById("okxHolds");
    if (!whaleBody) return;

    var whales = intel.whale_accumulation || [];
    whaleBody.innerHTML = whales.length
      ? whales
          .map(function (w) {
            var sig = w.buy_signal
              ? '<span class="signal-buy">AL</span>'
              : '<span class="signal-wait">—</span>';
            return (
              "<tr><td>" +
              esc(w.symbol) +
              "</td><td>" +
              w.wallet_count +
              (w.wallet_source === "helius"
                ? ' <span class="dex-boost" title="Helius">⛓</span>'
                : "") +
              "</td><td>x" +
              w.vol_spike +
              "</td><td>" +
              (w.chg_h1 >= 0 ? "+" : "") +
              w.chg_h1 +
              "%</td><td>" +
              sig +
              "</td></tr>"
            );
          })
          .join("")
      : '<tr class="empty-row"><td colspan="5">Henüz sinyal yok</td></tr>';

    function renderHolds(body, rows) {
      if (!body) return;
      body.innerHTML = rows.length
        ? rows
            .map(function (r) {
              return (
                "<tr><td>" +
                esc(r.symbol) +
                "</td><td>" +
                r.score +
                "</td><td>$" +
                Math.round(r.vol_24h_usd / 1e6) +
                "M</td><td class=\"hold-reason\">" +
                esc(r.reason) +
                "</td></tr>"
              );
            })
            .join("")
        : '<tr class="empty-row"><td colspan="4">Yükleniyor…</td></tr>';
    }
    renderHolds(binBody, intel.binance_holds || []);
    renderHolds(okxBody, intel.okx_holds || []);
  }

  function renderDecision(decision) {
    if (!decision || !decision.policy) return;
    var p = decision.policy;
    var entryEl = document.getElementById("decEntryMin");
    var exitEl = document.getElementById("decExitMax");
    var tpSlEl = document.getElementById("decTpSl");
    var ladderEl = document.getElementById("decExitLadder");
    var macroEl = document.getElementById("decMacro");
    var brainPenEl = document.getElementById("decBrainPenalty");
    var lastEl = document.getElementById("decisionLast");
    if (!entryEl) return;
    var entryMin = p.entry_score_min;
    if (decision.macro_avg != null && decision.macro_avg < p.macro_risk_off_score) {
      entryMin = p.entry_score_min + 10;
    }
    if (decision.brain_penalty) {
      entryMin += decision.brain_penalty;
    }
    entryEl.textContent = "≥ " + entryMin;
    if (exitEl) exitEl.textContent = "< " + p.exit_score_max;
    var ep = decision.exit_policy || (p.exit_ladder ? p.exit_ladder : null);
    if (ep) {
      tpSlEl.textContent = "SL " + ep.stop_loss_pct + "% · TP +" + ep.tp1_pct + "%";
      if (ladderEl) {
        ladderEl.textContent =
          "+" +
          ep.runner_arm_pct +
          "% runner · +" +
          ep.tp1_pct +
          "%/" +
          Math.round(ep.tp1_sell_frac * 100) +
          "% · +" +
          ep.tp2_pct +
          "% · trail " +
          ep.runner_trail_pct +
          "%";
      }
      var runnerEl = document.getElementById("decRunnerTrail");
      if (runnerEl) {
        runnerEl.textContent =
          "+" + ep.runner_arm_pct + "% arm · " + ep.runner_trail_pct + "% peak trail";
      }
    } else {
      tpSlEl.textContent = "+" + (p.take_profit_pct || 25) + "% / " + p.stop_loss_pct + "%";
      if (ladderEl) ladderEl.textContent = "—";
    }
    if (brainPenEl) {
      brainPenEl.textContent =
        decision.brain_penalty > 0 ? "+" + decision.brain_penalty : "0";
    }
    macroEl.textContent =
      decision.macro_avg != null
        ? decision.macro_avg + (decision.macro_avg < p.macro_risk_off_score ? " (risk-off)" : "")
        : "—";
    if (!decision.last) {
      lastEl.textContent = "Son karar: henüz tick yok";
      return;
    }
    var d = decision.last;
    var actionLabel =
      d.action === "enter"
        ? "AL"
        : d.action === "exit"
          ? "SAT"
          : d.action === "exit_partial"
            ? "KISMİ SAT"
            : d.action === "skip"
              ? "BEKLE"
              : "TUT";
    lastEl.textContent =
      "Son karar: " +
      actionLabel +
      (d.pair ? " · " + d.pair : "") +
      " — " +
      d.reason;
  }

  function fmtMcap(n) {
    if (!n) return "—";
    if (n >= 1e6) return "$" + (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return "$" + Math.round(n / 1e3) + "K";
    return "$" + Math.round(n);
  }

  function fmtVolUsd(n) {
    if (!n) return "—";
    if (n >= 1e6) return "$" + (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return "$" + Math.round(n / 1e3) + "K";
    return "$" + Math.round(n);
  }

  function fmtAgeHours(h) {
    if (h == null || h === undefined) return "—";
    if (h < 1) return Math.max(1, Math.round(h * 60)) + "m";
    if (h < 24) return Math.round(h) + "h";
    return Math.round(h / 24) + "d";
  }

  function chgPctCell(v) {
    var n = Number(v) || 0;
    var cls = n >= 0 ? "pos" : "neg";
    var sign = n >= 0 ? "+" : "";
    return '<td class="dex-chg ' + cls + '">' + sign + n.toFixed(1) + "%</td>";
  }

  function gateIcons(gates) {
    if (!gates) return "—";
    var parts = [];
    if (gates.score) parts.push(gates.score.ok ? "S✓" : "S✗");
    if (gates.momentum) parts.push(gates.momentum.ok ? "M✓" : "M✗");
    if (gates.filter) parts.push(gates.filter.ok ? "F✓" : "F✗");
    if (gates.edge) parts.push(gates.edge.ok ? "E✓" : "E✗");
    if (gates.smart_money) parts.push(gates.smart_money.ok ? "A✓" : "A✗");
    if (gates.safety) {
      if (gates.safety.ok === true) parts.push("G✓");
      else if (gates.safety.ok === false) parts.push("G✗");
      else parts.push("G?");
    }
    return parts.join(" ");
  }

  function growthStageClass(stage) {
    if (stage === "erken" || stage === "ivme") return "growth-hot";
    if (stage === "cex_erken") return "growth-cex";
    if (stage === "gec_pump") return "growth-late";
    return "growth-muted";
  }

  function renderGrowthPotential(growth) {
    var body = document.getElementById("growthBody");
    var summary = document.getElementById("growthSummary");
    if (!body) return;
    if (!growth || !growth.rows || !growth.rows.length) {
      body.innerHTML =
        '<tr class="empty-row"><td colspan="6">Artış adayı yok — tick bekleniyor</td></tr>';
      if (summary) summary.textContent = "0 aday";
      return;
    }
    if (summary) {
      summary.textContent =
        growth.count + " aday · erken/ivme öncelikli";
    }
    body.innerHTML = growth.rows
      .map(function (r) {
        var sig = (r.signals || []).slice(0, 3).join(" · ") || "—";
        var chain =
          r.chain === "cex"
            ? '<span class="growth-cex-tag">CEX</span>'
            : hudChainHtml(r.chain);
        return (
          '<tr class="hud-row-new"><td><span class="growth-stage ' +
          growthStageClass(r.stage) +
          '">' +
          esc(r.stage_label) +
          "</span></td><td>" +
          hudScoreHtml(r.upside_score) +
          '</td><td title="' +
          esc(r.name) +
          '"><strong>' +
          esc(r.name) +
          "</strong></td><td>" +
          chain +
          "</td><td>~" +
          (r.expected_move_pct != null ? r.expected_move_pct : "—") +
          '%</td><td class="entry-blocker" title="' +
          esc(sig) +
          '">' +
          esc(sig) +
          "</td></tr>"
        );
      })
      .join("");
  }

  function renderEntryDiagnostics(diag) {
    var body = document.getElementById("entryDiagBody");
    var summary = document.getElementById("entryDiagSummary");
    if (!body) return;
    if (!diag || !diag.rows || !diag.rows.length) {
      body.innerHTML = '<tr class="empty-row"><td colspan="5">Tick bekleniyor…</td></tr>';
      if (summary) summary.textContent = "—";
      return;
    }
    if (summary) {
      summary.textContent =
        diag.would_enter_count +
        "/" +
        diag.candidates +
        " aday geçer · tick #" +
        (diag.updated_tick || "—");
    }
    body.innerHTML = diag.rows
      .map(function (r) {
        var cex =
          r.cex_symbol && r.cex_hold_score
            ? r.cex_symbol + " " + r.cex_hold_score
            : "—";
        var conf = r.confluence || {};
        var confTxt =
          conf.score != null
            ? conf.score + (conf.layers && conf.layers.length ? " · " + conf.layers.join("+") : "")
            : "—";
        var rowCls = r.would_enter ? "hud-row-win" : "";
        return (
          '<tr class="' +
          rowCls +
          '"><td title="' +
          esc(r.pair) +
          '">' +
          esc(r.pair) +
          '</td><td class="text-right">' +
          r.score +
          '</td><td class="text-right">' +
          esc(String(confTxt)) +
          '</td><td>' +
          esc(cex) +
          '</td><td class="entry-blocker">' +
          esc(r.blocker || "—") +
          "</td></tr>"
        );
      })
      .join("");
  }

  function getSelectedScanModes() {
    var out = [];
    scanModeIds.forEach(function (id) {
      var el = document.getElementById("scanMode_" + id);
      if (el && el.checked) out.push(id);
    });
    return out;
  }

  function renderScanModes(modes) {
    var el = document.getElementById("scanModes");
    if (!el) return;
    el.innerHTML = modes
      .map(function (m) {
        var disabled = !m.enabled ? " disabled" : "";
        var checked = m.enabled && m.id !== "social" ? " checked" : "";
        var note = m.note
          ? ' <span class="scan-mode-note">' + esc(m.note) + "</span>"
          : "";
        return (
          '<label class="scan-mode' +
          (m.enabled ? "" : " scan-mode-off") +
          '"><input type="checkbox" id="scanMode_' +
          esc(m.id) +
          '"' +
          checked +
          disabled +
          "> " +
          esc(m.label) +
          note +
          "</label>"
        );
      })
      .join("");
  }

  function renderScanResults(data) {
    var body = document.getElementById("scanResults");
    var status = document.getElementById("scanStatus");
    if (!body || !status) return;
    if (!data.results || !data.results.length) {
      body.innerHTML = '<tr class="empty-row"><td colspan="5">Sonuç yok — modları değiştir</td></tr>';
      status.textContent = "Tarama bitti · 0 sonuç";
      return;
    }
    status.textContent =
      "Tarama bitti · " + data.count + " sonuç · modlar: " + (data.modes || []).join(", ");
    body.innerHTML = data.results
      .map(function (r) {
        var badge = r.tam_isabet
          ? '<span class="tam-badge" title="Tam isabet">🎯</span>'
          : "";
        return (
          '<tr class="hud-row-new"><td>' +
          badge +
          "</td><td><strong>" +
          esc(r.symbol) +
          "</strong></td><td>" +
          esc(r.exchange) +
          "</td><td>" +
          hudScoreHtml(r.score) +
          '</td><td class="hud-reason" title="' +
          esc(r.reason) +
          '">' +
          esc(r.reason) +
          "</td></tr>"
        );
      })
      .join("");
  }

  var brainKickDone = false;

  function scheduleBrainPoll(brain) {
    if (brain && brain.ready) {
      if (brainPollTimer) {
        clearInterval(brainPollTimer);
        brainPollTimer = null;
      }
      return;
    }
    if (brainPollTimer) return;
    brainPollTimer = setInterval(refresh, 4000);
  }

  function maybeAutoBrain(brain) {
    if (!brain || brain.ready) return;
    var msg = brain.message || "";
    if (msg.indexOf("devre dışı") >= 0 || msg.indexOf("HIBRIT_BRAIN_ENABLED=0") >= 0) {
      return;
    }
    if (brain.running) return;
    if (brainKickAttempts >= brainKickMax) return;
    brainKickAttempts += 1;
    fetchWithTimeout("/api/brain/run", { method: "POST" }, 15000).catch(function () {});
  }

  function setSaitoVisual(brain) {
    var visual = document.getElementById("saitoBrainVisual");
    if (!visual) return;
    visual.classList.remove("is-running", "is-ready");
    if (brain && brain.running) visual.classList.add("is-running");
    else if (brain && brain.ready !== false) visual.classList.add("is-ready");
  }

  function renderBrain(brain) {
    var status = document.getElementById("brainStatus");
    var grid = document.getElementById("brainGrid");
    var moves = document.getElementById("brainMoves");
    if (!status || !grid) return;
    setSaitoVisual(brain);
    status.className = "brain-status hud-status-bar";
    if (brain && brain.running) {
      status.className += " hud-pending";
      status.textContent = "Saito analiz ediyor… (20–90 sn)";
      return;
    }
    if (!brain || brain.ready === false) {
      status.className += " hud-pending";
      status.textContent =
        (brain && brain.message) || "Saito analiz kuyruğunda…";
      grid.innerHTML = "";
      if (moves) moves.innerHTML = "";
      return;
    }
    status.className += brain.degraded ? " hud-warn" : " hud-active";
    status.textContent =
      (brain.degraded ? "⚠ Yedek mod · " : "") +
      (brain.counterparty_thesis || "—");
    grid.innerHTML =
      '<div class="decision-item"><span class="decision-label">Rejim</span><span class="decision-value">' +
      esc(brain.regime) +
      "</span></div>" +
      '<div class="decision-item"><span class="decision-label">Mod</span><span class="decision-value">' +
      esc(brain.action_bias) +
      "</span></div>" +
      '<div class="decision-item"><span class="decision-label">Giriş cezası</span><span class="decision-value">+' +
      brain.entry_penalty +
      "</span></div>" +
      '<div class="decision-item"><span class="decision-label">Korku/Açgözlülük</span><span class="decision-value">' +
      (brain.fear_greed != null
        ? brain.fear_greed + (brain.fear_greed_label ? " " + esc(brain.fear_greed_label) : "")
        : "—") +
      "</span></div>";
    if (moves && brain.predicted_moves && brain.predicted_moves.length) {
      moves.innerHTML =
        "<strong>Karşı taraf hamleleri</strong><ul>" +
        brain.predicted_moves
          .map(function (m) {
            return (
              "<li><em>" +
              esc(m.actor) +
              "</em>: " +
              esc(m.action) +
              " (" +
              esc(m.impact) +
              ", %" +
              m.confidence +
              ")</li>"
            );
          })
          .join("") +
        "</ul>";
    } else if (moves) {
      moves.innerHTML = "";
    }
  }

  var defaultScanModes = [
    { id: "cex", label: "Binance/OKX Teknik", enabled: true },
    { id: "news", label: "Haber + Balina", enabled: true },
    { id: "whale", label: "Balina hareketi", enabled: true },
    { id: "derivatives", label: "Funding/OI Konfluans", enabled: true },
    { id: "grid", label: "Dinamik Spot Grid", enabled: true },
    { id: "social", label: "X / Sosyal (RED)", enabled: false, note: "Devre dışı" },
  ];

  async function loadScanModes() {
    try {
      var modes = await fetchWithTimeout("/api/scan/modes", {}, 15000).then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      });
      renderScanModes(modes);
    } catch (_) {
      renderScanModes(defaultScanModes);
      var el = document.getElementById("scanStatus");
      if (el) el.textContent = "Modlar yerel yüklendi — taramayı çalıştır";
    }
  }

  async function runAdvancedScan() {
    var btn = document.getElementById("runScanBtn");
    var status = document.getElementById("scanStatus");
    if (!btn || !status) return;
    var modes = getSelectedScanModes();
    if (!modes.length) {
      status.textContent = "En az bir mod seç";
      return;
    }
    btn.disabled = true;
    status.textContent = "Taranıyor… (20–60 sn)";
    try {
      var resp = await fetchWithTimeout(
        "/api/scan",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ modes: modes, limit: 15 }),
        },
        120000
      );
      if (!resp.ok) {
        var detail = "";
        try {
          detail = (await resp.json()).detail || "";
        } catch (_) {}
        throw new Error("HTTP " + resp.status + (detail ? ": " + detail : ""));
      }
      renderScanResults(await resp.json());
    } catch (e) {
      var errMsg = e.name === "AbortError" ? "Zaman aşımı (120 sn)" : e.message || "bilinmeyen";
      status.textContent = "Tarama hatası: " + errMsg;
      document.getElementById("scanResults").innerHTML =
        '<tr class="empty-row"><td colspan="5">Tarama başarısız</td></tr>';
    } finally {
      btn.disabled = false;
    }
  }

  function bindUI() {
    var phantomBtn = document.getElementById("phantomBtn");
    if (phantomBtn) phantomBtn.addEventListener("click", connectPhantom);
    var posPanel = document.getElementById("positionsPanel");
    if (posPanel) {
      posPanel.addEventListener("click", function (ev) {
        var btn = ev.target.closest(".qc-pos-btn");
        if (!btn) return;
        ev.preventDefault();
        manualPosition(btn.getAttribute("data-pool"), btn.getAttribute("data-act"));
      });
    }
    var themeBtn = document.getElementById("themeToggle");
    if (themeBtn) themeBtn.addEventListener("click", toggleTheme);
    var scanBtn = document.getElementById("runScanBtn");
    if (scanBtn) scanBtn.addEventListener("click", runAdvancedScan);
  }

  document.addEventListener("DOMContentLoaded", function () {
    initTheme();
    bindUI();
    loadScanModes();
    startPathFanLoop();
    refresh();
    setInterval(refresh, 30000);
    setInterval(processPhantomPending, 4000);
    initPhantom();
    fetchWithTimeout("/api/brain/run", { method: "POST" }, 15000).catch(function () {});
  });

  window.connectPhantom = connectPhantom;
})();
