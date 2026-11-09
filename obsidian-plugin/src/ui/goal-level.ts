import { tr } from "../i18n";

/** Display the protocol's four course levels; custom labels remain user content. */
export function goalLevelLabel(level: string): string {
  switch (level) {
    case "基础": case "Basic": case "basic": return tr("goal.level.basic");
    case "核心": case "Core": case "core": return tr("goal.level.core");
    case "进阶": case "Advanced": case "advanced": return tr("goal.level.advanced");
    case "拓展": case "Extension": case "extension": return tr("goal.level.extension");
    default: return level;
  }
}
