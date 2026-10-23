export type AssessmentGrade = {
  question_id: string; earned: number; maximum: number; reason: string;
  explanation?: string; correct_answer?: string[]; goal?: string; weaknesses?: string[];
  drawing_answer?: any; drawing_solution?: any;
};

export function gradeStatus(grade?: AssessmentGrade) {
  return !grade ? "pending" : grade.earned >= grade.maximum ? "correct" : grade.earned > 0 ? "partial" : "wrong";
}

export function answerText(answer: unknown, options: {id: string; text: string}[] = []) {
  if (typeof answer === "string") return answer;
  if (Array.isArray(answer)) return answer.map(id => {
    const option = options.find(value => value.id === id);
    return option ? `${id} · ${option.text}` : String(id);
  }).join("\n");
  return "";
}
