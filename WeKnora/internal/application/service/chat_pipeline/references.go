package chatpipeline

import (
	"context"
	"fmt"
	"strings"

	"github.com/Tencent/WeKnora/internal/logger"
	"github.com/Tencent/WeKnora/internal/modelcontext"
	"github.com/Tencent/WeKnora/internal/models/chat"
	"github.com/Tencent/WeKnora/internal/types"
)

// prepareMessagesWithModelContext replaces positional retrieval IDs with
// request-local model handles. Persisted rendered_content remains unchanged;
// public citations are expanded only when the request setting enables them.
func prepareMessagesWithModelContext(
	ctx context.Context,
	chatManage *types.ChatManage,
) ([]chat.Message, *modelcontext.Registry) {
	citationsEnabled := chatManage == nil || chatManage.CitationsEnabled()
	registry := modelcontext.NewRegistry(citationsEnabled)
	if chatManage == nil {
		return nil, registry
	}
	messages := prepareMessagesWithHistory(chatManage)
	if len(messages) > 0 {
		messages[0].Content = strings.TrimRight(messages[0].Content, " \t\r\n") + registry.ProtocolPrompt()
	}
	if len(chatManage.MergeResult) == 0 || len(messages) == 0 {
		return messages, registry
	}

	ordered := expandCitationSources(orderedPipelineReferences(chatManage))
	knowledgeResults := make([]*types.SearchResult, 0, len(ordered))
	knowledgeRows := make([]map[string]interface{}, 0, len(ordered))
	webRows := make([]map[string]interface{}, 0)
	for _, result := range ordered {
		if result == nil {
			continue
		}
		if isPipelineWebReference(result) {
			webRows = append(webRows, map[string]interface{}{
				"url":          result.ID,
				"title":        firstPipelineTitle(result),
				"snippet":      result.Content,
				"published_at": result.Metadata["published_at"],
			})
			continue
		}
		knowledgeResults = append(knowledgeResults, result)
		knowledgeRows = append(knowledgeRows, map[string]interface{}{
			"chunk_id":          result.ID,
			"knowledge_id":      result.KnowledgeID,
			"knowledge_base_id": result.KnowledgeBaseID,
			"knowledge_title":   firstPipelineTitle(result),
			"chunk_index":       result.ChunkIndex,
			"chunk_type":        result.ChunkType,
			"content":           getEnrichedPassageForChat(ctx, result),
			// A compact human-readable location (e.g. "第2页") derived from
			// the chunk's source locators. The model context renderer emits
			// it on the <chunk> tag; without it quick-answer only exposes
			// the title and chunk index, so any page number in the answer
			// would be invented.
			"source_location": formatSourceLocation(result.SourceLocators),
		})
	}
	registry.RegisterSearchResults(knowledgeResults)
	var contextParts []string
	if len(knowledgeRows) > 0 {
		data := map[string]interface{}{
			"display_type": "search_results",
			"results":      knowledgeRows,
		}
		// The ranked filter chain (rerank → merge → FILTER_TOP_K) may have
		// dropped candidates before this view was built. Carry the recorded
		// pool and the number of knowledge passages actually rendered, so the
		// model context can say the passages are a subset without restating
		// the pool as a knowledge-base match count.
		if truncation := chatManage.Truncation; truncation != nil {
			data["retrieval_shown"] = len(knowledgeRows)
			data["retrieval_candidates"] = truncation.Candidates
		}
		contextParts = append(contextParts, registry.ModelToolResult(&types.ToolResult{
			Success: true,
			Data:    data,
		}))
	}
	if len(webRows) > 0 {
		contextParts = append(contextParts, registry.ModelToolResult(&types.ToolResult{
			Success: true,
			Data: map[string]interface{}{
				"display_type": "web_search_results",
				"results":      webRows,
			},
		}))
	}
	modelContexts := strings.Join(contextParts, "\n")
	if strings.TrimSpace(modelContexts) == "" {
		modelContexts = "Retrieved source bodies could not be verified. Do not cite or infer facts from " +
			"unavailable retrieval evidence."
	}

	last := len(messages) - 1
	replaced := false
	for _, index := range []int{0, last} {
		if chatManage.RenderedContexts != "" && strings.Contains(messages[index].Content, chatManage.RenderedContexts) {
			messages[index].Content = strings.ReplaceAll(messages[index].Content, chatManage.RenderedContexts, modelContexts)
			replaced = true
		}
	}
	if !replaced {
		messages[last].Content = modelContexts + "\n\n" + messages[last].Content
	}
	if chatManage.ChatModelSupportsVision && len(chatManage.ContextImages) > 0 {
		count := min(len(chatManage.ContextImageChunkIDs), len(chatManage.ContextImages))
		ids := chatManage.ContextImageChunkIDs[:count]
		messages[last].Content += registry.ImageSourcesNote(ids, len(chatManage.Images))
	}
	return messages, registry
}

func isPipelineWebReference(result *types.SearchResult) bool {
	if result == nil {
		return false
	}
	return strings.EqualFold(result.ChunkType, string(types.ChunkTypeWebSearch)) ||
		strings.EqualFold(result.KnowledgeSource, "web_search")
}

func orderedPipelineReferences(chatManage *types.ChatManage) []*types.SearchResult {
	if chatManage == nil {
		return nil
	}
	if !chatManage.FAQPriorityEnabled {
		return chatManage.MergeResult
	}
	ordered := make([]*types.SearchResult, 0, len(chatManage.MergeResult))
	for _, result := range chatManage.MergeResult {
		if result != nil && result.ChunkType == string(types.ChunkTypeFAQ) {
			ordered = append(ordered, result)
		}
	}
	for _, result := range chatManage.MergeResult {
		if result != nil && result.ChunkType != string(types.ChunkTypeFAQ) {
			ordered = append(ordered, result)
		}
	}
	return ordered
}

func firstPipelineTitle(result *types.SearchResult) string {
	if result == nil {
		return ""
	}
	if result.KnowledgeTitle != "" {
		return result.KnowledgeTitle
	}
	return result.KnowledgeFilename
}

// formatSourceLocation renders the chunk's source locators as one short,
// human-readable label (e.g. "第2页") for the model context. Without it the
// model only sees the document title and chunk index and cannot truthfully
// state where a cited passage sits in the original file.
func formatSourceLocation(locators types.SourceLocators) string {
	parts := make([]string, 0, len(locators))
	seen := make(map[string]struct{}, len(locators))
	for _, loc := range locators {
		var label string
		switch loc.Type {
		case types.SourceLocatorPDF:
			if loc.Page > 0 {
				label = fmt.Sprintf("第%d页", loc.Page)
			}
		case types.SourceLocatorDocx:
			if loc.Block > 0 {
				label = fmt.Sprintf("第%d段", loc.Block)
			}
		case types.SourceLocatorSlide:
			if loc.Slide > 0 {
				label = fmt.Sprintf("第%d页幻灯片", loc.Slide)
			}
		case types.SourceLocatorSheet:
			switch {
			case loc.Sheet != "" && loc.RowStart > 0 && loc.RowEnd > loc.RowStart:
				label = fmt.Sprintf("%s 第%d-%d行", loc.Sheet, loc.RowStart, loc.RowEnd)
			case loc.Sheet != "" && loc.RowStart > 0:
				label = fmt.Sprintf("%s 第%d行", loc.Sheet, loc.RowStart)
			}
		case types.SourceLocatorText:
			if loc.Start > 0 || loc.End > 0 {
				label = fmt.Sprintf("字符 %d-%d", loc.Start, loc.End)
			}
		case types.SourceLocatorTime:
			if loc.StartMs > 0 || loc.EndMs > 0 {
				label = fmt.Sprintf("%.1f-%.1f 秒", float64(loc.StartMs)/1000, float64(loc.EndMs)/1000)
			}
		case types.SourceLocatorSection:
			if loc.Section > 0 {
				label = fmt.Sprintf("第%d节", loc.Section)
			}
		}
		if label == "" {
			continue
		}
		if loc.Title != "" {
			label += "（" + loc.Title + "）"
		}
		if _, ok := seen[label]; ok {
			continue
		}
		seen[label] = struct{}{}
		parts = append(parts, label)
	}
	return strings.Join(parts, "；")
}

// reportModelContextLeaks logs any durable identifier that survived encoding
// so the producing prompt or tool can be fixed; see modelcontext/leaks.go.
func reportModelContextLeaks(
	ctx context.Context, scope string, registry *modelcontext.Registry, messages []chat.Message,
) {
	leaks := registry.LeakedIdentifiers(messages)
	if len(leaks) == 0 {
		return
	}
	logger.Warnf(ctx, "[%s][ModelContext] %d message field(s) carry raw identifiers after encoding: %s",
		scope, len(leaks), modelcontext.SummarizeLeaks(leaks))
}
