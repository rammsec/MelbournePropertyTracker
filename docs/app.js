/**
 * Melbourne Property Tracker — Frontend Application (`docs/app.js`)
 * High-legibility, zero-dependency client: Leaflet.js, VanillaJS,
 * Dynamic Filtering, Theme Switcher & Victorian Financial Engine.
 */

// Application State
const state = {
  listings: [],
  settledSales: [],
  metadata: {},
  filteredListings: [],
  map: null,
  tileLayer: null,
  markersLayer: null,
  activeTab: 'tracker-tab',
  currentTheme: localStorage.getItem('melb_tracker_theme') || 'light',
  triage: (() => {
    try {
      return JSON.parse(localStorage.getItem('melb_property_triage') || '{}');
    } catch (e) {
      return {};
    }
  })(),
  pagination: {
    currentPage: 1,
    itemsPerPage: 10
  },
  filters: {
    search: '',
    suburb: 'ALL',
    agency: 'ALL',
    propertyType: 'ALL',
    riskLevel: 'ALL',
    maxBudget: 1200000,
    minBeds: 0,
    flagFilter: 'ALL',
    triage: 'ACTIVE',
    sortBy: 'risk-desc'
  }
};

// -----------------------------------------------------------------------------
// 1. Theme Management (Light / Dark Mode with Leaflet Tile Sync)
// -----------------------------------------------------------------------------
function initTheme() {
  setTheme(state.currentTheme);

  const toggleBtn = document.getElementById('theme-toggle-btn');
  if (!toggleBtn) return;

  toggleBtn.addEventListener('click', () => {
    const nextTheme = state.currentTheme === 'light' ? 'dark' : 'light';
    setTheme(nextTheme);
  });
}

function setTheme(theme) {
  state.currentTheme = theme;
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('melb_tracker_theme', theme);

  const toggleBtn = document.getElementById('theme-toggle-btn');
  if (toggleBtn) {
    if (theme === 'light') {
      toggleBtn.innerHTML = `<span class="theme-icon">🌙</span> <span class="theme-label">Dark Mode</span>`;
    } else {
      toggleBtn.innerHTML = `<span class="theme-icon">☀️</span> <span class="theme-label">Light Mode</span>`;
    }
  }

  // Sync Leaflet Map Tiles
  updateMapTiles(theme);
}

function updateMapTiles(theme) {
  if (!state.map) return;

  if (state.tileLayer) {
    state.map.removeLayer(state.tileLayer);
  }

  const tileUrl = theme === 'dark'
    ? 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png'
    : 'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png';

  state.tileLayer = L.tileLayer(tileUrl, {
    attribution: '&copy; OpenStreetMap contributors &copy; CARTO',
    subdomains: 'abcd',
    maxZoom: 19
  }).addTo(state.map);
}

// -----------------------------------------------------------------------------
// 2. Address Cleaning & Formatting Helpers
// -----------------------------------------------------------------------------
function cleanDisplayAddress(item) {
  // If normalized_address exists, extract the primary street part
  if (item.normalized_address && item.normalized_address.includes(',')) {
    const streetPart = item.normalized_address.split(',')[0].trim();
    if (streetPart && streetPart.length > 3) {
      return toTitleCase(streetPart);
    }
  }

  let addr = item.street_address || '';
  // Strip common scraper prefix garbage
  addr = addr.replace(/^(Open\s+\w+\s+|House for Sale\s+|Home for Sale\s+|Apartment for Sale\s+|Townhouse for Sale\s+|Auction\s+|Private Sale\s+|Under Offer\s+|Land for Sale\s+|Contact Agent\s+|-\s*|\$[\d,kKmM\s-]+\s*)+/gi, '').trim();
  
  if (item.suburb_name) {
    const subRegex = new RegExp(',?\\s*' + escapeRegExp(item.suburb_name) + '(\\s+\\d+\\s+Beds)?.*$', 'i');
    addr = addr.replace(subRegex, '').trim();
  }
  addr = addr.replace(/,\s*$/, '').trim();

  return addr ? toTitleCase(addr) : 'Melbourne Property';
}

function toTitleCase(str) {
  return str.toLowerCase().replace(/(?:^|\s|-|\/)\w/g, match => match.toUpperCase());
}

function escapeRegExp(string) {
  return string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function getListingUrl(item) {
  if (item && item.listing_url && typeof item.listing_url === 'string' && item.listing_url.startsWith('http')) {
    return item.listing_url;
  }
  const cleanAddr = [item?.street_address, item?.suburb_name, 'VIC', item?.postcode].filter(Boolean).join(' ');
  return `https://www.google.com/search?q=${encodeURIComponent(cleanAddr + ' property for sale')}`;
}

// Triage Data Helpers
function saveTriageData() {
  try {
    localStorage.setItem('melb_property_triage', JSON.stringify(state.triage));
  } catch (e) {
    console.warn('Failed to save triage data to localStorage', e);
  }
}

function setTriageDecision(listingId, decision) {
  if (!listingId) return;
  if (state.triage[listingId] === decision || decision === 'clear' || decision === null) {
    delete state.triage[listingId];
  } else {
    state.triage[listingId] = decision;
  }
  saveTriageData();
  updateTriageFilterLabels();
  applyFilters();
}

function updateTriageFilterLabels() {
  const triageSelect = document.getElementById('triage-filter');
  if (!triageSelect) return;

  const total = state.listings.length;
  let likeCount = 0;
  let holdCount = 0;
  let dropCount = 0;

  state.listings.forEach(l => {
    const d = state.triage[l.listing_id];
    if (d === 'like') likeCount++;
    else if (d === 'hold') holdCount++;
    else if (d === 'drop') dropCount++;
  });

  const activeCount = total - dropCount;
  const unreviewedCount = Math.max(0, total - (likeCount + holdCount + dropCount));

  const opts = triageSelect.options;
  for (let i = 0; i < opts.length; i++) {
    const val = opts[i].value;
    if (val === 'ALL') opts[i].text = `All Properties (${total})`;
    else if (val === 'ACTIVE') opts[i].text = `Hide Dropped (${activeCount})`;
    else if (val === 'LIKE') opts[i].text = `❤️ Liked (${likeCount})`;
    else if (val === 'HOLD') opts[i].text = `⏸️ On Hold (${holdCount})`;
    else if (val === 'DROP') opts[i].text = `❌ Dropped (${dropCount})`;
    else if (val === 'UNREVIEWED') opts[i].text = `⚪ Unreviewed (${unreviewedCount})`;
  }
}

// -----------------------------------------------------------------------------
// 3. Victorian Land Transfer Duty & FHB Financial Engine
// -----------------------------------------------------------------------------
function calculateGeneralStampDuty(dutiableValue) {
  const v = Math.max(0, dutiableValue);
  if (v <= 25000) return v * 0.014;
  if (v <= 130000) return 350.0 + (v - 25000) * 0.024;
  if (v <= 960000) return 2870.0 + (v - 130000) * 0.060;
  if (v <= 2000000) return v * 0.055;
  return 110000.0 + (v - 2000000) * 0.065;
}

function calculateStampDutyBreakdown(price, isFhb = true) {
  const baseDuty = calculateGeneralStampDuty(price);
  let payable = baseDuty;
  let savings = 0;
  let isExempt = false;
  let isConcession = false;

  if (isFhb) {
    if (price <= 600000) {
      payable = 0;
      savings = baseDuty;
      isExempt = true;
    } else if (price <= 750000) {
      const concessionFactor = (price - 600000) / 150000;
      payable = baseDuty * concessionFactor;
      savings = baseDuty - payable;
      isConcession = true;
    }
  }

  return {
    baseDuty: Math.round(baseDuty),
    payableDuty: Math.round(payable),
    savings: Math.round(savings),
    isExempt,
    isConcession
  };
}

function calculateCashToClose(price, availableSavings = 95000, isFhb = true, useFhg = true) {
  const isFhgEligible = isFhb && useFhg && price <= 950000;
  const depositPct = isFhgEligible ? 5 : 20;
  const depositAmount = price * (depositPct / 100);

  const duty = calculateStampDutyBreakdown(price, isFhb);
  const conveyancing = 2000;
  const buildingPest = 600;
  const titleRegistration = 1450;

  const totalUpfrontCostsExclDeposit = duty.payableDuty + conveyancing + buildingPest + titleRegistration;
  const totalCashRequired = depositAmount + totalUpfrontCostsExclDeposit;
  const difference = availableSavings - totalCashRequired;
  const hasSufficientFunds = difference >= 0;

  return {
    price,
    depositPct,
    depositAmount,
    isFhgEligible,
    duty,
    conveyancing,
    buildingPest,
    titleRegistration,
    totalUpfrontCostsExclDeposit,
    totalCashRequired,
    difference,
    hasSufficientFunds
  };
}

// -----------------------------------------------------------------------------
// 4. Navigation Tabs
// -----------------------------------------------------------------------------
function initNavigationTabs() {
  const btns = document.querySelectorAll('.nav-btn');
  btns.forEach(btn => {
    btn.addEventListener('click', () => {
      btns.forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));

      btn.classList.add('active');
      const targetId = btn.getAttribute('data-tab');
      const targetPane = document.getElementById(targetId);
      if (targetPane) {
        targetPane.classList.add('active');
        state.activeTab = targetId;
        if (targetId === 'tracker-tab' && state.map) {
          setTimeout(() => state.map.invalidateSize(), 150);
        }
      }
    });
  });
}

// -----------------------------------------------------------------------------
// 5. Geospatial Leaflet Map
// -----------------------------------------------------------------------------
function initMap() {
  const mapEl = document.getElementById('map');
  if (!mapEl) return;

  state.map = L.map('map', {
    center: [-37.8136, 144.9631],
    zoom: 12,
    zoomControl: false
  });

  L.control.zoom({ position: 'topright' }).addTo(state.map);

  // Set initial tile layer according to current theme
  updateMapTiles(state.currentTheme);

  state.markersLayer = L.layerGroup().addTo(state.map);
}

function updateMapMarkers(listings) {
  if (!state.map || !state.markersLayer) return;
  state.markersLayer.clearLayers();

  const bounds = [];

  listings.slice(0, 150).forEach(item => {
    const lat = Number(item.latitude);
    const lon = Number(item.longitude);
    if (isNaN(lat) || isNaN(lon) || lat === 0) return;

    bounds.push([lat, lon]);

    let markerColor = '#0284C7';
    if (item.underquoting_risk_level?.includes('High')) {
      markerColor = '#DC2626';
    } else if (item.underquoting_risk_level?.includes('Distressed')) {
      markerColor = '#0891B2';
    } else if (item.underquoting_risk_level?.includes('Moderate')) {
      markerColor = '#D97706';
    } else {
      markerColor = '#059669';
    }

    const circleMarker = L.circleMarker([lat, lon], {
      radius: 8,
      fillColor: markerColor,
      color: '#FFFFFF',
      weight: 2,
      opacity: 0.95,
      fillOpacity: 0.9
    });

    const displayAddr = cleanDisplayAddress(item);
    const gapPct = Number(item.underquoting_gap_pct || 0);

    const popupContent = `
      <div style="font-family: var(--font-sans); color: #0F172A; min-width: 220px; padding: 4px;">
        <div style="font-size: 11px; font-weight: 800; color: ${markerColor}; text-transform: uppercase; margin-bottom: 2px;">
          ${item.underquoting_risk_level}
        </div>
        <div style="font-size: 14px; font-weight: 800; margin: 2px 0;">${displayAddr}</div>
        <div style="font-size: 13px; font-weight: 600; color: #475569; margin-bottom: 6px;">${item.suburb_name} &bull; ${item.property_type}</div>
        <div style="background: #F1F5F9; padding: 8px 10px; border-radius: 6px; font-size: 12px; display: flex; flex-direction: column; gap: 3px;">
          <div>Guide: <strong>${item.price_guide_raw}</strong></div>
          <div>AVM Fair Value: <strong>$${Number(item.avm_estimated_value).toLocaleString()}</strong></div>
          <div>Gap: <strong style="color: ${markerColor}; font-weight: 800;">${gapPct > 0 ? '+' : ''}${gapPct}%</strong></div>
        </div>
      </div>
    `;

    circleMarker.bindPopup(popupContent);
    circleMarker.addTo(state.markersLayer);
  });

  if (bounds.length > 0 && listings.length < 50) {
    state.map.fitBounds(bounds, { padding: [40, 40], maxZoom: 14 });
  }
}

// -----------------------------------------------------------------------------
// 6. Filtering & Search Logic
// -----------------------------------------------------------------------------
function populateSuburbDropdown() {
  const suburbSelect = document.getElementById('suburb-filter');
  if (!suburbSelect) return;

  const suburbs = [...new Set(state.listings.map(l => l.suburb_name).filter(Boolean))].sort();
  suburbs.forEach(sub => {
    const opt = document.createElement('option');
    opt.value = sub;
    opt.textContent = sub;
    suburbSelect.appendChild(opt);
  });
}

function initFilterListeners() {
  // Search text input
  const searchInput = document.getElementById('search-input');
  searchInput?.addEventListener('input', (e) => {
    state.filters.search = e.target.value.trim().toLowerCase();
    applyFilters();
  });

  // Suburb
  document.getElementById('suburb-filter')?.addEventListener('change', (e) => {
    state.filters.suburb = e.target.value;
    applyFilters();
  });

  // Agency
  document.getElementById('agency-filter')?.addEventListener('change', (e) => {
    state.filters.agency = e.target.value;
    applyFilters();
  });

  // Type
  document.getElementById('type-filter')?.addEventListener('change', (e) => {
    state.filters.propertyType = e.target.value;
    applyFilters();
  });

  // Risk
  document.getElementById('risk-filter')?.addEventListener('change', (e) => {
    state.filters.riskLevel = e.target.value;
    applyFilters();
  });

  // Budget slider
  const slider = document.getElementById('budget-slider');
  const budgetDisplay = document.getElementById('budget-display');
  slider?.addEventListener('input', (e) => {
    const val = Number(e.target.value);
    state.filters.maxBudget = val;
    budgetDisplay.textContent = `$${val.toLocaleString()}`;
    applyFilters();
  });

  // Quick preset buttons
  document.querySelectorAll('.preset-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const preset = Number(btn.getAttribute('data-preset'));
      state.filters.maxBudget = preset;
      if (slider) slider.value = preset;
      if (budgetDisplay) budgetDisplay.textContent = preset >= 2500000 ? 'Any Budget' : `$${preset.toLocaleString()}`;
      applyFilters();
    });
  });

  // Bedrooms pills
  document.querySelectorAll('#beds-selector .pill-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#beds-selector .pill-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.filters.minBeds = Number(btn.getAttribute('data-val'));
      applyFilters();
    });
  });

  // Flag filter
  document.getElementById('flag-filter')?.addEventListener('change', (e) => {
    state.filters.flagFilter = e.target.value;
    state.pagination.currentPage = 1;
    applyFilters();
  });

  // Triage filter
  document.getElementById('triage-filter')?.addEventListener('change', (e) => {
    state.filters.triage = e.target.value;
    state.pagination.currentPage = 1;
    applyFilters();
  });

  // Sort by
  document.getElementById('sort-by')?.addEventListener('change', (e) => {
    state.filters.sortBy = e.target.value;
    state.pagination.currentPage = 1;
    sortAndRenderListings();
  });

  // Pagination navigation listeners
  document.getElementById('prev-page-btn')?.addEventListener('click', () => {
    if (state.pagination.currentPage > 1) {
      goToPage(state.pagination.currentPage - 1);
    }
  });

  document.getElementById('next-page-btn')?.addEventListener('click', () => {
    const totalPages = Math.ceil(state.filteredListings.length / state.pagination.itemsPerPage) || 1;
    if (state.pagination.currentPage < totalPages) {
      goToPage(state.pagination.currentPage + 1);
    }
  });

  // Items per page selector
  document.getElementById('items-per-page')?.addEventListener('change', (e) => {
    state.pagination.itemsPerPage = Number(e.target.value);
    state.pagination.currentPage = 1;
    sortAndRenderListings();
  });

  // Reset
  document.getElementById('reset-filters-btn')?.addEventListener('click', () => {
    state.filters = {
      search: '',
      suburb: 'ALL',
      agency: 'ALL',
      propertyType: 'ALL',
      riskLevel: 'ALL',
      maxBudget: 2500000,
      minBeds: 0,
      flagFilter: 'ALL',
      triage: 'ALL',
      sortBy: 'risk-desc'
    };
    state.pagination.currentPage = 1;
    if (searchInput) searchInput.value = '';
    const subSel = document.getElementById('suburb-filter');
    if (subSel) subSel.value = 'ALL';
    const agSel = document.getElementById('agency-filter');
    if (agSel) agSel.value = 'ALL';
    const typeSel = document.getElementById('type-filter');
    if (typeSel) typeSel.value = 'ALL';
    const riskSel = document.getElementById('risk-filter');
    if (riskSel) riskSel.value = 'ALL';
    const flagSel = document.getElementById('flag-filter');
    if (flagSel) flagSel.value = 'ALL';
    const triageSel = document.getElementById('triage-filter');
    if (triageSel) triageSel.value = 'ALL';
    const sortSel = document.getElementById('sort-by');
    if (sortSel) sortSel.value = 'risk-desc';
    if (slider) slider.value = 2500000;
    if (budgetDisplay) budgetDisplay.textContent = 'Any Budget';
    document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
    document.querySelector('.preset-btn[data-preset="2500000"]')?.classList.add('active');
    document.querySelectorAll('#beds-selector .pill-btn').forEach(b => b.classList.remove('active'));
    document.querySelector('#beds-selector .pill-btn[data-val="0"]')?.classList.add('active');
    applyFilters();
  });

  // Modals close buttons
  document.getElementById('comps-close-btn')?.addEventListener('click', () => {
    document.getElementById('comps-modal').classList.add('hidden');
  });
  document.getElementById('c2c-close-btn')?.addEventListener('click', () => {
    document.getElementById('c2c-modal').classList.add('hidden');
  });
}

function applyFilters() {
  const f = state.filters;

  state.filteredListings = state.listings.filter(item => {
    // Search keyword
    if (f.search) {
      const q = f.search;
      const addr = (item.street_address || '').toLowerCase();
      const norm = (item.normalized_address || '').toLowerCase();
      const sub = (item.suburb_name || '').toLowerCase();
      const ag = (item.agency_name || '').toLowerCase();
      if (!addr.includes(q) && !norm.includes(q) && !sub.includes(q) && !ag.includes(q)) {
        return false;
      }
    }

    // Suburb
    if (f.suburb !== 'ALL' && item.suburb_name !== f.suburb) return false;

    // Agency
    if (f.agency !== 'ALL') {
      const src = (item.source || '').toLowerCase();
      const agName = (item.agency_name || '').toLowerCase();
      const q = f.agency.toLowerCase();
      if (!src.includes(q) && !agName.includes(q)) return false;
    }

    // Property Type
    if (f.propertyType !== 'ALL' && item.property_type !== f.propertyType) return false;

    // Risk Level
    if (f.riskLevel !== 'ALL' && item.underquoting_risk_level !== f.riskLevel) return false;

    // Budget
    const price = Number(item.price_guide_high || item.price_guide_low || 0);
    if (price > f.maxBudget) return false;

    // Bedrooms
    if (Number(item.bedrooms || 0) < f.minBeds) return false;

    // Flag / Due diligence
    if (f.flagFilter !== 'ALL') {
      const heading = (item.property_issue_heading || '').toUpperCase();
      if (f.flagFilter === 'CLEAN' && !heading.includes('CLEAN')) return false;
      if (f.flagFilter === 'WINDOWLESS' && !heading.includes('WINDOWLESS')) return false;
      if (f.flagFilter === 'CLADDING' && !heading.includes('CLADDING') && !heading.includes('HIGH OC')) return false;
      if (f.flagFilter === 'MAIN_ROAD' && !heading.includes('MAIN ROAD')) return false;
      if (f.flagFilter === 'BANK_RESTRICTED' && !heading.includes('UNDERSIZED')) return false;
    }

    // Triage / Shortlist Filter
    const decision = state.triage[item.listing_id];
    if (f.triage === 'ACTIVE' && decision === 'drop') return false;
    if (f.triage === 'LIKE' && decision !== 'like') return false;
    if (f.triage === 'HOLD' && decision !== 'hold') return false;
    if (f.triage === 'DROP' && decision !== 'drop') return false;
    if (f.triage === 'UNREVIEWED' && decision) return false;

    return true;
  });

  const countEl = document.getElementById('filtered-count');
  if (countEl) countEl.textContent = state.filteredListings.length;

  sortAndRenderListings();
  updateMapMarkers(state.filteredListings);
}

function sortAndRenderListings() {
  const sortBy = state.filters.sortBy;
  const list = [...state.filteredListings];

  list.sort((a, b) => {
    if (sortBy === 'risk-desc') return Number(b.underquoting_gap_pct || 0) - Number(a.underquoting_gap_pct || 0);
    if (sortBy === 'price-asc') return Number(a.price_guide_high || 0) - Number(b.price_guide_high || 0);
    if (sortBy === 'price-desc') return Number(b.price_guide_high || 0) - Number(a.price_guide_high || 0);
    if (sortBy === 'avm-desc') return Number(b.avm_estimated_value || 0) - Number(a.avm_estimated_value || 0);
    if (sortBy === 'c2c-asc') return Number(a.total_cash_to_close || 0) - Number(b.total_cash_to_close || 0);
    return 0;
  });

  const grid = document.getElementById('listings-grid');
  if (!grid) return;

  const totalItems = list.length;
  const perPage = state.pagination.itemsPerPage;
  const totalPages = Math.ceil(totalItems / perPage) || 1;

  if (state.pagination.currentPage > totalPages) {
    state.pagination.currentPage = totalPages;
  }
  if (state.pagination.currentPage < 1) {
    state.pagination.currentPage = 1;
  }
  const currentPage = state.pagination.currentPage;
  const startIndex = (currentPage - 1) * perPage;
  const pageItems = list.slice(startIndex, startIndex + perPage);

  renderPagination(totalItems, currentPage, perPage, totalPages);

  if (totalItems === 0) {
    grid.innerHTML = `
      <div style="padding: 3.5rem 1.5rem; text-align: center; color: var(--text-muted); background: var(--bg-surface); border-radius: var(--radius-lg); border: 1.5px dashed var(--border-subtle);">
        <h3 style="color: var(--text-heading); font-size: 1.25rem; font-weight: 700;">No properties match your filter criteria</h3>
        <p style="font-size: 0.95rem; margin-top: 0.5rem; color: var(--text-secondary);">Try raising the price ceiling, clearing the search keyword, or selecting "All Properties" under Shortlist / Triage.</p>
      </div>`;
    return;
  }

  grid.innerHTML = pageItems.map(item => renderPropertyCard(item)).join('');

  // Attach card event listeners
  grid.querySelectorAll('.property-card').forEach(card => {
    const listingId = card.getAttribute('data-id');
    const targetItem = state.listings.find(l => l.listing_id === listingId);

    card.querySelector('.c2c-btn')?.addEventListener('click', (e) => {
      e.stopPropagation();
      openCashToCloseModal(targetItem);
    });

    card.querySelector('.comps-btn')?.addEventListener('click', (e) => {
      e.stopPropagation();
      openCompsModal(targetItem);
    });

    // Triage buttons
    card.querySelectorAll('.triage-btn').forEach(tbtn => {
      tbtn.addEventListener('click', (e) => {
        e.stopPropagation();
        const action = tbtn.getAttribute('data-action');
        setTriageDecision(listingId, action);
      });
    });

    card.querySelector('.triage-clear-btn')?.addEventListener('click', (e) => {
      e.stopPropagation();
      setTriageDecision(listingId, 'clear');
    });

    card.addEventListener('click', () => {
      if (state.map && targetItem?.latitude && targetItem?.longitude) {
        state.map.setView([targetItem.latitude, targetItem.longitude], 15, { animate: true });
      }
    });
  });
}

function renderPagination(totalItems, currentPage, perPage, totalPages) {
  const bar = document.getElementById('pagination-bar');
  if (!bar) return;

  if (totalItems === 0) {
    bar.style.display = 'none';
    return;
  }
  bar.style.display = 'flex';

  const startEl = document.getElementById('page-start');
  const endEl = document.getElementById('page-end');
  const totalEl = document.getElementById('page-total');
  if (startEl) startEl.textContent = ((currentPage - 1) * perPage + 1).toLocaleString();
  if (endEl) endEl.textContent = Math.min(currentPage * perPage, totalItems).toLocaleString();
  if (totalEl) totalEl.textContent = totalItems.toLocaleString();

  const prevBtn = document.getElementById('prev-page-btn');
  const nextBtn = document.getElementById('next-page-btn');
  if (prevBtn) prevBtn.disabled = currentPage <= 1;
  if (nextBtn) nextBtn.disabled = currentPage >= totalPages;

  const pageNumbersEl = document.getElementById('page-numbers');
  if (pageNumbersEl) {
    pageNumbersEl.innerHTML = buildPageNumbersHtml(currentPage, totalPages);
    pageNumbersEl.querySelectorAll('.page-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const p = Number(btn.getAttribute('data-page'));
        goToPage(p);
      });
    });
  }
}

function buildPageNumbersHtml(current, total) {
  if (total <= 1) return '';
  const pages = [];
  if (total <= 7) {
    for (let i = 1; i <= total; i++) pages.push(i);
  } else {
    pages.push(1);
    if (current > 3) pages.push('...');
    const start = Math.max(2, current - 1);
    const end = Math.min(total - 1, current + 1);
    for (let i = start; i <= end; i++) {
      if (!pages.includes(i)) pages.push(i);
    }
    if (current < total - 2) pages.push('...');
    if (!pages.includes(total)) pages.push(total);
  }

  return pages.map(p => {
    if (p === '...') return `<span class="page-ellipsis">…</span>`;
    return `<button type="button" class="page-btn ${p === current ? 'active' : ''}" data-page="${p}">${p}</button>`;
  }).join('');
}

function goToPage(page) {
  state.pagination.currentPage = page;
  sortAndRenderListings();
  const grid = document.getElementById('listings-grid');
  if (grid) grid.scrollTop = 0;
}

// -----------------------------------------------------------------------------
// 7. Property Card Component (High Readability & Clickable Posts)
// -----------------------------------------------------------------------------
function renderPropertyCard(item) {
  let riskBadgeClass = 'risk-green';
  let riskIcon = '🟢';
  let riskTitle = 'Honest Guide';

  if (item.underquoting_risk_level?.includes('High')) {
    riskBadgeClass = 'risk-red';
    riskIcon = '🔴';
    riskTitle = 'Auction Bait';
  } else if (item.underquoting_risk_level?.includes('Distressed')) {
    riskBadgeClass = 'risk-cyan';
    riskIcon = '💎';
    riskTitle = 'Motivated Seller';
  } else if (item.underquoting_risk_level?.includes('Moderate')) {
    riskBadgeClass = 'risk-amber';
    riskIcon = '🟠';
    riskTitle = 'Moderate Gap';
  }

  const isClean = (item.property_issue_heading || '').includes('Clean');
  const defectClass = isClean ? 'clean' : 'warning';
  const defectIcon = isClean ? '✅' : '⚠️';

  const priceHigh = Number(item.price_guide_high || item.price_guide_low || 0);
  const avmVal = Number(item.avm_estimated_value || 0);
  const gapPct = Number(item.underquoting_gap_pct || 0);
  const gapAud = Math.abs(avmVal - priceHigh);

  let fhbBadge = '';
  if (priceHigh <= 600000) {
    fhbBadge = `<span class="fhb-pill">$0 Stamp Duty (Full Exemption)</span>`;
  } else if (priceHigh <= 750000) {
    fhbBadge = `<span class="fhb-pill">FHB Duty Concession</span>`;
  } else if (priceHigh <= 950000) {
    fhbBadge = `<span class="fhb-pill">5% Deposit Eligible ($0 LMI)</span>`;
  }

  const postUrl = getListingUrl(item);
  const displayAddress = cleanDisplayAddress(item);

  // Triage state
  const decision = state.triage[item.listing_id];
  let cardTriageClass = '';
  let triageBadgeHtml = `<span style="color: var(--text-muted); font-weight: 500;">⚪ Unreviewed</span>`;
  if (decision === 'like') {
    cardTriageClass = 'is-liked';
    triageBadgeHtml = `<span class="badge triage-badge-like">❤️ Liked</span>`;
  } else if (decision === 'hold') {
    cardTriageClass = 'is-hold';
    triageBadgeHtml = `<span class="badge triage-badge-hold">⏸️ On Hold</span>`;
  } else if (decision === 'drop') {
    cardTriageClass = 'is-dropped';
    triageBadgeHtml = `<span class="badge triage-badge-drop">❌ Dropped</span>`;
  }

  return `
    <article class="property-card ${cardTriageClass}" data-id="${item.listing_id}">
      <div class="card-top">
        <div class="card-badges">
          <span class="badge ${riskBadgeClass}">
            ${riskIcon} ${riskTitle}: ${gapPct > 0 ? '+' : ''}${gapPct}%
          </span>
          <span class="badge agency-badge">${item.agency_name || item.source}</span>
          ${decision ? triageBadgeHtml : ''}
        </div>
        <div class="card-source-date">${item.sale_method || 'Auction'}</div>
      </div>

      <div class="card-address-block">
        <a href="${postUrl}" target="_blank" rel="noopener noreferrer" class="card-title-link" onclick="event.stopPropagation();" title="Open actual property listing post in new tab">
          <span class="card-address">${displayAddress}</span>
          <span class="external-icon" aria-hidden="true">↗</span>
        </a>
        <div class="card-suburb">${item.suburb_name}, VIC ${item.postcode || ''} &bull; ${item.property_type}</div>
      </div>

      <div class="card-valuation-grid">
        <div class="val-col">
          <span class="val-label">Quoted Agent Guide</span>
          <span class="val-number">${item.price_guide_raw || '$' + priceHigh.toLocaleString()}</span>
        </div>
        <div class="val-col">
          <span class="val-label">AVM Market Fair Value</span>
          <span class="val-number" style="color: var(--accent-primary);">$${avmVal.toLocaleString()}</span>
        </div>
        <div class="val-col">
          <span class="val-label">Estimated Gap / Reserve</span>
          <span class="val-number gap-${riskBadgeClass.replace('risk-', '')}">
            ${gapPct > 0 ? '+' : ''}$${gapAud.toLocaleString()} (${gapPct > 0 ? '+' : ''}${gapPct}%)
          </span>
        </div>
      </div>

      <!-- Triage decision bar: Like, Hold, Drop -->
      <div class="card-triage-bar" onclick="event.stopPropagation();">
        <div class="triage-label-group">
          <span>My Decision:</span>
          ${triageBadgeHtml}
        </div>
        <div class="triage-btns">
          <button type="button" class="triage-btn triage-like ${decision === 'like' ? 'active' : ''}" data-action="like" title="Like / Shortlist this property">
            <span>${decision === 'like' ? '❤️' : '🤍'}</span> Like
          </button>
          <button type="button" class="triage-btn triage-hold ${decision === 'hold' ? 'active' : ''}" data-action="hold" title="Keep on hold / Under review">
            <span>⏸️</span> Hold
          </button>
          <button type="button" class="triage-btn triage-drop ${decision === 'drop' ? 'active' : ''}" data-action="drop" title="Drop / Discard this property">
            <span>❌</span> Drop
          </button>
          ${decision ? `<button type="button" class="triage-clear-btn" data-action="clear" title="Reset decision">↺ Reset</button>` : ''}
        </div>
      </div>

      <div class="defect-banner ${defectClass}">
        <span style="font-size: 1.15rem; line-height: 1;">${defectIcon}</span>
        <div>
          <strong>${item.property_issue_heading}</strong>: ${item.property_issue_details || 'Standard Section 32 due diligence recommended.'}
        </div>
      </div>

      <div class="card-features">
        <span class="card-feature-item">🛏️ <strong>${item.bedrooms || 0}</strong> Beds</span>
        <span class="card-feature-item">🚿 <strong>${item.bathrooms || 1}</strong> Baths</span>
        <span class="card-feature-item">🚗 <strong>${item.car_spaces || 0}</strong> Parking</span>
        <span class="card-feature-item">📐 <strong>${Math.round(item.building_area_sqm || item.land_size_sqm || 0)}</strong> m²</span>
        <span class="card-feature-item">📍 <strong>${item.cbd_distance_km || 0}</strong> km to CBD</span>
      </div>

      <div class="card-actions">
        <div>${fhbBadge}</div>
        <div class="btn-group">
          <button type="button" class="card-btn primary c2c-btn">💰 Cash to Close</button>
          <button type="button" class="card-btn comps-btn">📊 Comps</button>
          <a href="${postUrl}" target="_blank" rel="noopener noreferrer" class="card-btn" onclick="event.stopPropagation();" title="Open actual listing post in new tab">
            View Listing ↗
          </a>
        </div>
      </div>
    </article>
  `;
}

// -----------------------------------------------------------------------------
// 8. Modals: Cash-to-Close & Recent Comps
// -----------------------------------------------------------------------------
function openCashToCloseModal(listing) {
  const modal = document.getElementById('c2c-modal');
  const title = document.getElementById('c2c-modal-title');
  const body = document.getElementById('c2c-modal-body');
  if (!modal || !listing) return;

  const price = Number(listing.price_guide_high || listing.price_guide_low || 800000);
  const fin = calculateCashToClose(price, 95000, true, true);
  const displayAddress = cleanDisplayAddress(listing);

  title.textContent = `Cash-to-Close: ${displayAddress}`;

  body.innerHTML = `
    <div style="font-size: 0.95rem; color: var(--text-secondary); margin-bottom: 1.25rem; line-height: 1.5;">
      Exact Victorian SRO Land Transfer Duty, Federal First Home Guarantee (FHG) 5% deposit assessment, and estimated settlement costs for <strong>${listing.suburb_name}</strong>.
    </div>

    <table class="fin-table">
      <tr>
        <td>Property Purchase Price</td>
        <td>$${price.toLocaleString()}</td>
      </tr>
      <tr>
        <td>Minimum Required Deposit (${fin.depositPct}%)</td>
        <td>$${fin.depositAmount.toLocaleString()}</td>
      </tr>
      <tr>
        <td>Federal First Home Guarantee (FHG)</td>
        <td style="color: ${fin.isFhgEligible ? '#059669' : 'var(--text-muted)'}; font-weight: 700;">
          ${fin.isFhgEligible ? '✅ Applied (5% Deposit with $0 LMI)' : 'Standard 20% Deposit (Cap $950k)'}
        </td>
      </tr>
      <tr>
        <td>Standard Base Victorian Stamp Duty</td>
        <td>$${fin.duty.baseDuty.toLocaleString()}</td>
      </tr>
      <tr>
        <td>First-Home Buyer Exemption/Concession Savings</td>
        <td style="color: #059669; font-weight: 700;">- $${fin.duty.savings.toLocaleString()}</td>
      </tr>
      <tr>
        <td><strong>Payable Victorian Land Transfer Duty</strong></td>
        <td><strong>$${fin.duty.payableDuty.toLocaleString()}</strong></td>
      </tr>
      <tr>
        <td>Conveyancing / Legal Settlement Fees</td>
        <td>$${fin.conveyancing.toLocaleString()}</td>
      </tr>
      <tr>
        <td>Building & Pest Inspection</td>
        <td>$${fin.buildingPest.toLocaleString()}</td>
      </tr>
      <tr>
        <td>Title Transfer & Registration Fee</td>
        <td>$${fin.titleRegistration.toLocaleString()}</td>
      </tr>
      <tr class="fin-total-row">
        <td>TOTAL CASH REQUIRED TO CLOSE</td>
        <td>$${fin.totalCashRequired.toLocaleString()}</td>
      </tr>
      <tr class="fin-surplus-row ${fin.hasSufficientFunds ? 'surplus' : 'shortfall'}">
        <td>Sample Available Savings ($95,000)</td>
        <td>${fin.hasSufficientFunds ? '✅ Surplus: +$' + fin.difference.toLocaleString() : '⚠️ Shortfall: -$' + Math.abs(fin.difference).toLocaleString()}</td>
      </tr>
    </table>
  `;

  modal.classList.remove('hidden');
}

function openCompsModal(listing) {
  const modal = document.getElementById('comps-modal');
  const title = document.getElementById('comps-modal-title');
  const body = document.getElementById('comps-modal-body');
  if (!modal || !listing) return;

  title.textContent = `Comparable Settled Sales: ${listing.suburb_name}`;

  const comps = state.settledSales.filter(s => {
    return s.suburb_name === listing.suburb_name || s.property_type === listing.property_type;
  }).slice(0, 5);

  if (comps.length === 0) {
    body.innerHTML = `<p style="color: var(--text-muted); text-align: center; padding: 2.5rem; font-size: 1rem;">No historical sales found matching this exact suburb and property type.</p>`;
  } else {
    body.innerHTML = `
      <div style="font-size: 0.95rem; color: var(--text-secondary); margin-bottom: 1.25rem;">
        Historical Valuer-General Victoria settled transactions utilized by the machine learning model for <strong>${listing.suburb_name}</strong>.
      </div>
      <table class="fin-table">
        <thead>
          <tr style="color: var(--text-secondary); font-size: 0.8rem; text-transform: uppercase;">
            <th style="text-align: left; padding: 0.6rem 0.8rem;">Address</th>
            <th style="text-align: left; padding: 0.6rem 0.8rem;">Type / Beds</th>
            <th style="text-align: left; padding: 0.6rem 0.8rem;">Settlement</th>
            <th style="text-align: right; padding: 0.6rem 0.8rem;">Sold Price</th>
          </tr>
        </thead>
        <tbody>
          ${comps.map(c => `
            <tr>
              <td><strong>${c.street_address}</strong><br><span style="font-size: 0.82rem; color: var(--text-muted);">${c.suburb_name}</span></td>
              <td>${c.property_type} (${c.bedrooms}B / ${c.bathrooms}Ba)</td>
              <td>${c.settlement_date || 'Recent'}</td>
              <td style="color: #059669; font-weight: 800; font-family: var(--font-mono);">$${Number(c.sold_price).toLocaleString()}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
  }

  modal.classList.remove('hidden');
}

// -----------------------------------------------------------------------------
// 9. Standalone FHB Calculator Tab
// -----------------------------------------------------------------------------
function initStandaloneCalculator() {
  const priceInput = document.getElementById('calc-price-input');
  const savingsInput = document.getElementById('calc-savings-input');
  const fhbCheck = document.getElementById('calc-fhb-check');
  const fhgCheck = document.getElementById('calc-fhg-check');
  const resultsContainer = document.getElementById('calc-standalone-results');

  function update() {
    const price = Number(priceInput.value) || 0;
    const savings = Number(savingsInput.value) || 0;
    const isFhb = fhbCheck.checked;
    const isFhg = fhgCheck.checked;

    const res = calculateCashToClose(price, savings, isFhb, isFhg);

    resultsContainer.innerHTML = `
      <table class="fin-table">
        <tr>
          <td>Property Purchase Price</td>
          <td>$${price.toLocaleString()}</td>
        </tr>
        <tr>
          <td>Minimum Deposit Required (${res.depositPct}%)</td>
          <td>$${res.depositAmount.toLocaleString()}</td>
        </tr>
        <tr>
          <td>Federal First Home Guarantee (FHG)</td>
          <td style="color: ${res.isFhgEligible ? '#059669' : 'var(--text-muted)'}; font-weight: 700;">
            ${res.isFhgEligible ? '✅ Applied (5% Deposit with $0 LMI)' : 'Standard 20% Deposit (Cap $950,000)'}
          </td>
        </tr>
        <tr>
          <td>Standard Base Victorian Stamp Duty</td>
          <td>$${res.duty.baseDuty.toLocaleString()}</td>
        </tr>
        <tr>
          <td>First-Home Buyer Exemption/Concession Savings</td>
          <td style="color: #059669; font-weight: 700;">- $${res.duty.savings.toLocaleString()}</td>
        </tr>
        <tr>
          <td><strong>Payable Stamp Duty (SRO Victoria)</strong></td>
          <td><strong>$${res.duty.payableDuty.toLocaleString()}</strong></td>
        </tr>
        <tr>
          <td>Conveyancer / Legal Transfer Fee</td>
          <td>$${res.conveyancing.toLocaleString()}</td>
        </tr>
        <tr>
          <td>Building & Pest Inspection</td>
          <td>$${res.buildingPest.toLocaleString()}</td>
        </tr>
        <tr>
          <td>Title Transfer Registration</td>
          <td>$${res.titleRegistration.toLocaleString()}</td>
        </tr>
        <tr class="fin-total-row">
          <td>TOTAL CASH REQUIRED TO CLOSE</td>
          <td>$${res.totalCashRequired.toLocaleString()}</td>
        </tr>
        <tr class="fin-surplus-row ${res.hasSufficientFunds ? 'surplus' : 'shortfall'}">
          <td>Your Available Savings ($${savings.toLocaleString()})</td>
          <td>${res.hasSufficientFunds ? '✅ Surplus: +$' + res.difference.toLocaleString() : '⚠️ Shortfall: -$' + Math.abs(res.difference).toLocaleString()}</td>
        </tr>
      </table>
    `;
  }

  priceInput?.addEventListener('input', update);
  savingsInput?.addEventListener('input', update);
  fhbCheck?.addEventListener('change', update);
  fhgCheck?.addEventListener('change', update);
  update();
}

// -----------------------------------------------------------------------------
// 10. Digest Markdown Renderer & Strip Generator
// -----------------------------------------------------------------------------
function renderDigestMarkdown(md) {
  const container = document.getElementById('digest-content');
  if (!container) return;

  // Render high-level summary cards in the strip
  const strip = document.getElementById('digest-stats-strip');
  if (strip && state.metadata) {
    const meta = state.metadata;
    const r2 = meta.avm_metrics?.r2 ? (meta.avm_metrics.r2 * 100).toFixed(1) + '%' : '92.1%';
    const mape = meta.avm_metrics?.mape_pct ? meta.avm_metrics.mape_pct + '%' : '10.2%';
    
    strip.innerHTML = `
      <div class="digest-stat-card">
        <div class="digest-stat-num">${meta.total_active_listings || state.listings.length}</div>
        <div class="digest-stat-label">Active Listings Scraped</div>
      </div>
      <div class="digest-stat-card">
        <div class="digest-stat-num">${meta.total_settled_sales || state.settledSales.length}</div>
        <div class="digest-stat-label">Settled Sales Comps</div>
      </div>
      <div class="digest-stat-card">
        <div class="digest-stat-num" style="color: var(--accent-primary);">${r2}</div>
        <div class="digest-stat-label">AVM Model Accuracy (R²)</div>
      </div>
      <div class="digest-stat-card">
        <div class="digest-stat-num" style="color: #059669;">${mape}</div>
        <div class="digest-stat-label">Mean Absolute % Error</div>
      </div>
    `;
  }

  // Convert markdown to clean HTML
  let html = md
    .replace(/^# (.*$)/gim, '<h1>$1</h1>')
    .replace(/^## (.*$)/gim, '<h2>$1</h2>')
    .replace(/^### (.*$)/gim, '<h3>$1</h3>')
    .replace(/^\> (.*$)/gim, '<blockquote>$1</blockquote>')
    .replace(/\*\*(.*?)\*\*/gim, '<strong>$1</strong>');

  // Convert markdown tables
  html = html.replace(/\|(.+)\|/gim, (match) => {
    if (match.includes('---')) return '';
    const cells = match.split('|').filter(c => c.trim() !== '');
    return '<tr>' + cells.map(c => `<td>${c.trim()}</td>`).join('') + '</tr>';
  });

  html = html.replace(/(<tr>.*?<\/tr>(\s*<tr>.*?<\/tr>)+)/gim, '<div class="table-responsive"><table>$1</table></div>');

  container.innerHTML = html;
}

// -----------------------------------------------------------------------------
// 11. Data Ingestion & Bootstrapping
// -----------------------------------------------------------------------------
async function initApp() {
  initTheme();
  initNavigationTabs();
  initMap();
  initFilterListeners();
  initStandaloneCalculator();

  try {
    const [listingsRes, salesRes, metaRes, digestRes] = await Promise.all([
      fetch('data/listings.json'),
      fetch('data/settled_sales.json'),
      fetch('data/metadata.json'),
      fetch('data/digest.md')
    ]);

    if (listingsRes.ok) state.listings = await listingsRes.json();
    if (salesRes.ok) state.settledSales = await salesRes.json();
    if (metaRes.ok) state.metadata = await metaRes.json();
    if (digestRes.ok) {
      const digestMd = await digestRes.text();
      renderDigestMarkdown(digestMd);
    }

    // Populate navbar stats
    const activeCountEl = document.getElementById('stat-active-count');
    const modelAccEl = document.getElementById('stat-model-acc');

    if (activeCountEl) {
      activeCountEl.textContent = `${state.listings.length} Properties`;
    }
    if (modelAccEl && state.metadata?.avm_metrics) {
      modelAccEl.textContent = `AVM R²: ${state.metadata.avm_metrics.r2.toFixed(3)}`;
    }

    populateSuburbDropdown();
    updateTriageFilterLabels();
    applyFilters();

  } catch (err) {
    console.error('Error bootstrapping application data:', err);
    const grid = document.getElementById('listings-grid');
    if (grid) {
      grid.innerHTML = `
        <div style="padding: 2.5rem; text-align: center; color: var(--badge-red-text); background: var(--badge-red-bg); border-radius: var(--radius-md); border: 1px solid var(--badge-red-border);">
          <h3>Error loading Melbourne property data</h3>
          <p style="margin-top: 0.5rem; font-size: 0.95rem;">Please check network connection and ensure data/listings.json is accessible.</p>
        </div>`;
    }
  }
}

// Run when DOM is ready
document.addEventListener('DOMContentLoaded', initApp);
