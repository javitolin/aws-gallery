import { readFileSync } from "fs";
const src = readFileSync("site/app.js", "utf8");
// Pull the pure helpers out and exercise them without a DOM.
const body = src.slice(src.indexOf("const parentOf"), src.indexOf("function chip("));
let allItems = [
  { category: "Ofer Trip" }, { category: "Ofer Trip/שיאן" }, { category: "Ofer Trip/שיאן/deep" },
  { category: "Ofer Trip/החומה הסינית" }, { category: "Berlin 2017" }, { category: null },
];
const fn = new Function("allItems", body + "; return { parentOf, leafOf, inBranch, childrenOf };");
const { parentOf, leafOf, inBranch, childrenOf } = fn(allItems);

const eq = (label, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  console.log(`  ${ok ? "ok  " : "FAIL"} ${label}${ok ? "" : ` got ${JSON.stringify(got)} want ${JSON.stringify(want)}`}`);
  if (!ok) process.exitCode = 1;
};
eq("parentOf nested", parentOf("Ofer Trip/שיאן"), "Ofer Trip");
eq("parentOf top level", parentOf("Berlin 2017"), null);
eq("leafOf", leafOf("Ofer Trip/שיאן"), "שיאן");
eq("top level children", [...childrenOf(null).keys()].sort(), ["Berlin 2017", "Ofer Trip", "Other"]);
eq("children of a branch", [...childrenOf("Ofer Trip").keys()].sort(),
   ["Ofer Trip/החומה הסינית", "Ofer Trip/שיאן"]);
eq("branch includes descendants", allItems.filter(i => inBranch(i, "Ofer Trip")).length, 4);
eq("leaf branch", allItems.filter(i => inBranch(i, "Ofer Trip/שיאן")).length, 2);
eq("no prefix bleed", allItems.filter(i => inBranch(i, "Ofer")).length, 0);
