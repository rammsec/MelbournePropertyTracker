/**
 * Melbourne Property Tracker — Main Web Application (`docs/app.js`)
 * Zero-dependency frontend: Leaflet.js, Vanillajs, Dynamic Filtering & Victorian Financial Engine.
 */

// Application State
const state = {
  listings: [],
  settledSales: [],
  metadata: {},
  filteredListings: [],
  map: null,
  markersLayer: null,
  activeTab: 'tracker-tab',
  selectedListing: null,
  filters: {
    suburb: 'ALL',
    agency: 'ALL',
    propertyType: 'ALL',
    riskLevel: 'ALL',
    maxBudget: 1200000,
    minBeds: 0,
    flagFilter: 'ALL',
    sortBy: 'risk-desc'
  }
};

// -----------------------------------------------------------------------------
// 1. Victorian Land Transfer Duty & FHB Financial Calculator (Client-side)
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
      payable = baseDuty * ((price - 600000) / 150000);
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

function calculateCashToClose(price, savings, isFhb = true, applyFhg = true) {
  const duty = calculateStampDutyBreakdown(price, isFhb);
  const isFhgEligible = isFhb && applyFhg && price <= 950000;
  const depositPct = isFhgEligible ? 0.05 : 0.20;
  const depositAmount = Math.round(price * depositPct);

  const conveyancing = 2000;
  const buildingPest = 600;
  const titleRegistration = 1450;
  const upfrontCosts = duty.payableDuty + conveyancing + buildingPest + titleRegistration;
  const totalCashRequired = depositAmount + upfrontCosts;
  const difference = Math.round(savings - totalCashRequired);

  return {
    price,
    savings,
    depositPct: depositPct * 100,
    depositAmount,
    isFhgEligible,
    duty,
    conveyancing,
    buildingPest,
    titleRegistration,
    upfrontCosts,
    totalCashRequired,
    difference,
    hasSufficientFunds: difference >= 0
  };
}

// -----------------------------------------------------------------------------
// 2. Initialization & Data Fetching
// -----------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', async () => {
  initNavigationTabs();
  initMap();
  initFilterListeners();
  initStandaloneCalculator();
  await loadData();
});

async function loadData() {
  try {
    const [listingsRes, salesRes, metaRes, digestRes] = await Promise.all([
      fetch('data/listings.json'),
      fetch('data/settled_sales.json').catch(() => ({ ok: false })),
      fetch('data/metadata.json').catch(() => ({ ok: false })),
      fetch('data/digest.md').catch(() => ({ ok: false }))
    ]);

    if (!listingsRes.ok) throw new Error('Could not load data/listings.json');
    state.listings = await listingsRes.json();

    if (salesRes.ok) state.settledSales = await salesRes.json();
    if (metaRes.ok) {
      state.metadata = await metaRes.json();
      updateMetaStats();
    }

    if (digestRes.ok) {
      const digestMd = await digestRes.text();
      renderDigestMarkdown(digestMd);
    }

    populateSuburbDropdown();
    applyFilters();
  } catch (err) {
    console.error('Failed to load real estate datasets:', err);
    document.getElementById('listings-grid').innerHTML = `
      <div style="padding: 2rem; text-align: center; color: var(--accent-red);">
        <h3>⚠️ Unable to load live listings</h3>
        <p>Ensure <code>data/listings.json</code> has been generated via GitHub Actions.</p>
      </div>`;
  }
}

function updateMetaStats() {
  const countEl = document.getElementById('stat-active-count');
  const accEl = document.getElementById('stat-model-acc');
  if (countEl) countEl.textContent = `${state.metadata.total_active_listings || state.listings.length} Active Listings`;
  if (accEl && state.metadata.avm_metrics) {
    accEl.textContent = `AVM R²: ${Number(state.metadata.avm_metrics.r2).toFixed(3)}`;
  }
}

// -----------------------------------------------------------------------------
// 3. Navigation Tabs
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
// 4. Geospatial Leaflet Map
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

  // CartoDB Dark Matter tiles
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; OpenStreetMap contributors &copy; CARTO',
    subdomains: 'abcd',
    maxZoom: 19
  }).addTo(state.map);

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

    let markerColor = '#38BDF8';
    if (item.risk_color_hex) {
      markerColor = item.risk_color_hex;
    } else if (item.underquoting_risk_level?.includes('High')) {
      markerColor = '#EF4444';
    } else if (item.underquoting_risk_level?.includes('Distressed')) {
      markerColor = '#06B6D4';
    } else if (item.underquoting_risk_level?.includes('Moderate')) {
      markerColor = '#F59E0B';
    } else {
      markerColor = '#10B981';
    }

    const circleMarker = L.circleMarker([lat, lon], {
      radius: 7,
      fillColor: markerColor,
      color: '#FFFFFF',
      weight: 1.5,
      opacity: 0.9,
      fillOpacity: 0.85
    });

    const popupContent = `
      <div style="font-family: var(--font-sans); color: #0F172A; min-width: 200px;">
        <div style="font-size: 11px; font-weight: 700; color: ${markerColor}; text-transform: uppercase;">
          ${item.underquoting_risk_level}
        </div>
        <div style="font-size: 13px; font-weight: 700; margin: 3px 0;">${item.street_address}</div>
        <div style="font-size: 12px; color: #475569;">${item.suburb_name} (${item.property_type})</div>
        <div style="margin-top: 6px; font-size: 12px; background: #F1F5F9; padding: 4px 6px; border-radius: 4px;">
          <div>Guide: <strong>${item.price_guide_raw}</strong></div>
          <div>AVM Fair Value: <strong>$${Number(item.avm_estimated_value).toLocaleString()}</strong></div>
          <div>Gap: <strong style="color: ${markerColor};">${item.underquoting_gap_pct > 0 ? '+' : ''}${item.underquoting_gap_pct}%</strong></div>
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
// 5. Filtering & Rendering
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
    applyFilters();
  });

  // Sort by
  document.getElementById('sort-by')?.addEventListener('change', (e) => {
    state.filters.sortBy = e.target.value;
    sortAndRenderListings();
  });

  // Reset
  document.getElementById('reset-filters-btn')?.addEventListener('click', () => {
    state.filters = {
      suburb: 'ALL',
      agency: 'ALL',
      propertyType: 'ALL',
      riskLevel: 'ALL',
      maxBudget: 2500000,
      minBeds: 0,
      flagFilter: 'ALL',
      sortBy: 'risk-desc'
    };
    document.getElementById('suburb-filter').value = 'ALL';
    document.getElementById('agency-filter').value = 'ALL';
    document.getElementById('type-filter').value = 'ALL';
    document.getElementById('risk-filter').value = 'ALL';
    document.getElementById('flag-filter').value = 'ALL';
    slider.value = 2500000;
    budgetDisplay.textContent = 'Any Budget';
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

    // Risk
    if (f.riskLevel !== 'ALL' && item.underquoting_risk_level !== f.riskLevel) return false;

    // Budget (checks price_guide_high or price_guide_low)
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

    return true;
  });

  document.getElementById('filtered-count').textContent = state.filteredListings.length;
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

  if (list.length === 0) {
    grid.innerHTML = `
      <div style="padding: 3rem 1rem; text-align: center; color: var(--text-muted);">
        <h3>No properties match the selected criteria</h3>
        <p style="font-size: 0.9rem; margin-top: 0.5rem;">Try raising the budget cap or broadening the suburb filter.</p>
      </div>`;
    return;
  }

  grid.innerHTML = list.map(item => renderPropertyCard(item)).join('');

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

    card.addEventListener('click', () => {
      if (state.map && targetItem?.latitude && targetItem?.longitude) {
        state.map.setView([targetItem.latitude, targetItem.longitude], 15, { animate: true });
      }
    });
  });
}

function renderPropertyCard(item) {
  let riskBadgeClass = 'risk-green';
  if (item.underquoting_risk_level?.includes('High')) riskBadgeClass = 'risk-red';
  else if (item.underquoting_risk_level?.includes('Distressed')) riskBadgeClass = 'risk-cyan';
  else if (item.underquoting_risk_level?.includes('Moderate')) riskBadgeClass = 'risk-amber';

  const isClean = (item.property_issue_heading || '').includes('Clean');
  const defectClass = isClean ? 'clean' : 'warning';
  const defectIcon = isClean ? '✅' : '⚠️';

  const priceHigh = Number(item.price_guide_high || item.price_guide_low || 0);
  const avmVal = Number(item.avm_estimated_value || 0);
  const gapPct = Number(item.underquoting_gap_pct || 0);

  let fhbBadge = '';
  if (priceHigh <= 600000) {
    fhbBadge = `<span class="fhb-pill">$0 Stamp Duty (100% Free)</span>`;
  } else if (priceHigh <= 750000) {
    fhbBadge = `<span class="fhb-pill">FHB Duty Concession</span>`;
  } else if (priceHigh <= 950000) {
    fhbBadge = `<span class="fhb-pill">5% Deposit Eligible ($0 LMI)</span>`;
  }

  const externalLinkHtml = item.listing_url ? `
    <a href="${item.listing_url}" target="_blank" rel="noopener noreferrer" class="card-btn" onclick="event.stopPropagation();">
      Portal Link ↗
    </a>` : '';

  return `
    <div class="property-card" data-id="${item.listing_id}">
      <div class="card-top">
        <div class="card-badges">
          <span class="badge ${riskBadgeClass}">
            ${item.underquoting_risk_level} (${gapPct > 0 ? '+' : ''}${gapPct}%)
          </span>
          <span class="badge agency-badge">${item.agency_name || item.source}</span>
        </div>
        <div class="card-source-date">${item.sale_method || 'Auction'}</div>
      </div>

      <div>
        <div class="card-address">${item.street_address}</div>
        <div class="card-suburb">${item.suburb_name}, VIC ${item.postcode || ''}</div>
      </div>

      <div class="defect-banner ${defectClass}">
        <span>${defectIcon}</span>
        <span><strong>${item.property_issue_heading}</strong>: ${item.property_issue_details || ''}</span>
      </div>

      <div class="card-valuation-grid">
        <div class="val-col">
          <span class="val-label">Quoted Guide</span>
          <span class="val-number">${item.price_guide_raw}</span>
        </div>
        <div class="val-col">
          <span class="val-label">AVM Fair Value</span>
          <span class="val-number" style="color: var(--accent-blue);">$${avmVal.toLocaleString()}</span>
        </div>
        <div class="val-col">
          <span class="val-label">Underquoting Gap</span>
          <span class="val-number gap-${riskBadgeClass.replace('risk-', '')}">
            ${gapPct > 0 ? '+' : ''}${gapPct}%
          </span>
        </div>
      </div>

      <div class="card-features">
        <span class="card-feature-item">🛏️ ${item.bedrooms || 0} Beds</span>
        <span class="card-feature-item">🚿 ${item.bathrooms || 1} Baths</span>
        <span class="card-feature-item">🚗 ${item.car_spaces || 0} Parking</span>
        <span class="card-feature-item">📐 ${Math.round(item.building_area_sqm || item.land_size_sqm || 0)} m²</span>
        <span class="card-feature-item">📍 ${item.cbd_distance_km || 0} km to CBD</span>
      </div>

      <div class="card-actions">
        <div>${fhbBadge}</div>
        <div class="btn-group">
          <button type="button" class="card-btn primary c2c-btn">💰 Cash to Close</button>
          <button type="button" class="card-btn comps-btn">📊 Comps</button>
          ${externalLinkHtml}
        </div>
      </div>
    </div>
  `;
}

// -----------------------------------------------------------------------------
// 6. Modals (Cash-to-Close & Comps)
// -----------------------------------------------------------------------------
function openCashToCloseModal(listing) {
  const modal = document.getElementById('c2c-modal');
  const title = document.getElementById('c2c-modal-title');
  const body = document.getElementById('c2c-modal-body');
  if (!modal || !listing) return;

  const price = Number(listing.price_guide_high || listing.price_guide_low || 800000);
  const fin = calculateCashToClose(price, 95000, true, true);

  title.textContent = `Cash-to-Close: ${listing.street_address}`;

  body.innerHTML = `
    <div style="font-size: 0.88rem; color: var(--text-secondary); margin-bottom: 1.25rem;">
      Full Victorian Government Land Transfer Duty, Federal 5% First Home Guarantee (FHG) eligibility, and settlement costs for <strong>${listing.suburb_name}</strong>.
    </div>

    <table class="fin-table">
      <tr>
        <td>Property Purchase Price</td>
        <td>$${price.toLocaleString()}</td>
      </tr>
      <tr>
        <td>Required Minimum Deposit (${fin.depositPct}%)</td>
        <td>$${fin.depositAmount.toLocaleString()}</td>
      </tr>
      <tr>
        <td>Federal First Home Guarantee (FHG)</td>
        <td style="color: ${fin.isFhgEligible ? 'var(--accent-green)' : 'var(--text-muted)'};">
          ${fin.isFhgEligible ? '✅ Applied (5% Deposit, $0 LMI)' : 'Standard 20% Deposit (Cap $950k)'}
        </td>
      </tr>
      <tr>
        <td>Standard Victorian Stamp Duty (Base)</td>
        <td>$${fin.duty.baseDuty.toLocaleString()}</td>
      </tr>
      <tr>
        <td>First-Home Buyer Exemption/Concession Savings</td>
        <td style="color: var(--accent-green);">- $${fin.duty.savings.toLocaleString()}</td>
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

  // Find nearest sales matching suburb and property type
  const comps = state.settledSales.filter(s => {
    return s.suburb_name === listing.suburb_name || s.property_type === listing.property_type;
  }).slice(0, 5);

  if (comps.length === 0) {
    body.innerHTML = `<p style="color: var(--text-muted); text-align: center; padding: 2rem;">No nearby historical sales found for this precinct.</p>`;
  } else {
    body.innerHTML = `
      <div style="font-size: 0.85rem; color: var(--text-secondary); margin-bottom: 1rem;">
        Historical Valuer-General Victoria settled transactions utilized by the AVM model for <strong>${listing.suburb_name}</strong>.
      </div>
      <table class="fin-table">
        <thead>
          <tr style="color: var(--text-muted); font-size: 0.75rem; text-transform: uppercase;">
            <th style="text-align: left; padding: 0.5rem;">Address</th>
            <th style="text-align: left; padding: 0.5rem;">Type / Beds</th>
            <th style="text-align: left; padding: 0.5rem;">Settlement</th>
            <th style="text-align: right; padding: 0.5rem;">Sold Price</th>
          </tr>
        </thead>
        <tbody>
          ${comps.map(c => `
            <tr>
              <td><strong>${c.street_address}</strong><br><span style="font-size: 0.75rem; color: var(--text-muted);">${c.suburb_name}</span></td>
              <td>${c.property_type} (${c.bedrooms}B / ${c.bathrooms}Ba)</td>
              <td>${c.settlement_date || 'Recent'}</td>
              <td style="color: var(--accent-green); font-weight: 700;">$${Number(c.sold_price).toLocaleString()}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
  }

  modal.classList.remove('hidden');
}

// -----------------------------------------------------------------------------
// 7. Standalone FHB Calculator Tab
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
          <td>Purchase Price</td>
          <td>$${price.toLocaleString()}</td>
        </tr>
        <tr>
          <td>Minimum Deposit Required (${res.depositPct}%)</td>
          <td>$${res.depositAmount.toLocaleString()}</td>
        </tr>
        <tr>
          <td>Federal First Home Guarantee (FHG)</td>
          <td style="color: ${res.isFhgEligible ? 'var(--accent-green)' : 'var(--text-muted)'};">
            ${res.isFhgEligible ? '✅ Applied (5% Deposit with $0 LMI)' : 'Standard 20% Deposit (Cap $950,000)'}
          </td>
        </tr>
        <tr>
          <td>Standard Base Victorian Stamp Duty</td>
          <td>$${res.duty.baseDuty.toLocaleString()}</td>
        </tr>
        <tr>
          <td>First-Home Buyer Exemption/Concession Savings</td>
          <td style="color: var(--accent-green);">- $${res.duty.savings.toLocaleString()}</td>
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
// 8. Digest Markdown Renderer
// -----------------------------------------------------------------------------
function renderDigestMarkdown(md) {
  const container = document.getElementById('digest-content');
  if (!container) return;

  // Simple Markdown table and blockquote parser
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
  html = html.replace(/(<tr>.*?<\/tr>(\s*<tr>.*?<\/tr>)+)/gim, '<table>$1</table>');

  container.innerHTML = html;
}
