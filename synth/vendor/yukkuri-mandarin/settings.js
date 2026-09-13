export class NonHanziModes {
  constructor({
    globalMode = "ignore",
    globalReplace = "",
    enMode = null,
    enReplace = null,
    jaMode = null,
    jaReplace = null,
    pcMode = null,
    pcReplace = null,
    otherMode = null,
    otherReplace = null,
  } = {}) {
    this.enMode = enMode ?? globalMode;
    this.enReplace = enReplace ?? globalReplace;
    this.jaMode = jaMode ?? globalMode;
    this.jaReplace = jaReplace ?? globalReplace;
    this.pcMode = pcMode ?? globalMode;
    this.pcReplace = pcReplace ?? globalReplace;
    this.otherMode = otherMode ?? globalMode;
    this.otherReplace = otherReplace ?? globalReplace;
  }
}
