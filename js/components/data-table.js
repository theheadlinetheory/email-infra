/**
 * Sortable data table with loading/error/empty states.
 */

export function dataTable({ columns, rows, sortKey = null, sortDir = 'asc', onSort, onRowClick, emptyMessage = 'No data' }) {
  const table = document.createElement('table');
  table.className = 'data-table';

  // Header
  const thead = document.createElement('thead');
  const headerRow = document.createElement('tr');
  for (const col of columns) {
    const th = document.createElement('th');
    th.textContent = col.label;
    if (col.sortable && onSort) {
      th.style.cursor = 'pointer';
      if (col.key === sortKey) {
        th.textContent += sortDir === 'asc' ? ' ↑' : ' ↓';
      }
      th.addEventListener('click', () => onSort(col.key));
    }
    if (col.width) th.style.width = col.width;
    headerRow.appendChild(th);
  }
  thead.appendChild(headerRow);
  table.appendChild(thead);

  // Body
  const tbody = document.createElement('tbody');
  if (!rows || rows.length === 0) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = columns.length;
    td.style.textAlign = 'center';
    td.style.padding = '24px';
    td.style.color = 'var(--text-muted)';
    td.textContent = emptyMessage;
    tr.appendChild(td);
    tbody.appendChild(tr);
  } else {
    for (const row of rows) {
      const tr = document.createElement('tr');
      if (onRowClick) {
        tr.style.cursor = 'pointer';
        tr.addEventListener('click', () => onRowClick(row));
      }
      for (const col of columns) {
        const td = document.createElement('td');
        if (col.render) {
          const content = col.render(row);
          if (typeof content === 'string') {
            td.innerHTML = content;
          } else if (content instanceof HTMLElement) {
            td.appendChild(content);
          }
        } else {
          td.textContent = row[col.key] ?? '';
        }
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
  }
  table.appendChild(tbody);
  return table;
}

export function dataTableSkeleton(colCount = 4, rowCount = 5) {
  const el = document.createElement('div');
  for (let i = 0; i < rowCount; i++) {
    const row = document.createElement('div');
    row.style.display = 'flex';
    row.style.gap = '12px';
    row.style.padding = '10px 0';
    row.style.borderBottom = '1px solid var(--border-light)';
    for (let j = 0; j < colCount; j++) {
      const cell = document.createElement('div');
      cell.className = 'skeleton skeleton-line';
      cell.style.flex = '1';
      cell.style.height = '14px';
      row.appendChild(cell);
    }
    el.appendChild(row);
  }
  return el;
}
