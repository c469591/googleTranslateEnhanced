# -*- coding:UTF-8 -*-

import globalPluginHandler
import api
import sys
import config
import logHandler
import scriptHandler
import urllib.request
import urllib.parse
import re
import html
import speech
import braille
import codecs
import os
import json
import treeInterceptorHandler
import textInfos
import globalVars
import shutil
import ui
import tones
import threading
import time
import random
import concurrent.futures

#匹配所有中日韓文字、標點符號，以及夾雜在中間的空白、數字、半形冒號
chRe = re.compile(r"[\u4E00-\u9FFF\u2000-\u206F\u3000-\u303F\uFF00-\uFFEF]+"
    r"([\s\d:]*"
    r"[\u4E00-\u9FFF\u2000-\u206F\u3000-\u303F\uFF00-\uFFEF]*)*")

#假如原始內容有被分割，此列表存放他們的翻譯結果。
transResultList = []

#假如此資料夾沒有資料檔，而之前的資料檔存在，就把他們搬回來。
path = os.path.dirname(__file__)
files = []
for file in os.listdir(path):
    if re.match(r'transCache [a-zA-Z_]+ .json', file):
        files.append(file)
if not files:
    for file in os.listdir(globalVars.appArgs.configPath):
        if re.match(r'transCache [a-zA-Z_]+ .json', file):
            shutil.move(os.path.join(globalVars.appArgs.configPath, file), os.path.join(path, file))

toLang = config.conf['general']['language']
userAgentString = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'
originalSpeak = None
lastSequence = None
enableTranslate = False
transCache = {}
lastTranslatedText = ''
fileName = None
uiSpeak = False

# 添加翻译失败状态标志
translationFailed = False
# 添加翻译失败重试计数器
MAX_RETRIES = 3
failCount = 0
# 使用线程池而不是为每个小片段创建新线程
MAX_WORKERS = 5
threadPool = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)

def googleTranslate(text):
    """使用Google翻译API端点翻译文本，直接连接不使用系统代理"""
    global failCount, translationFailed
    if not text.strip():
        return ""
    
    # 检查长度，对于极短文本（如单个字符）直接返回
    if len(text.strip()) <= 1:
        return text
    
    urlText = urllib.parse.quote(text)
    # 使用translate_a/single API端点
    # 构建请求参数
    params = {
        'client': 'gtx',  # 使用gtx作为客户端标识
        'sl': 'auto',     # 源语言自动检测
        'tl': toLang,     # 目标语言
        'dt': 't',        # 只请求翻译文本
        'q': urlText      # 要翻译的文本
    }
    
    # 构建URL
    query_string = '&'.join(f"{k}={v}" for k, v in params.items() if k != 'q')
    url = f'https://translate.googleapis.com/translate_a/single?{query_string}&q={urlText}'
    
    # 添加重试机制
    for attempt in range(MAX_RETRIES):
        try:
            # 创建一个不使用代理的处理器
            no_proxy_handler = urllib.request.ProxyHandler({})
            # 创建一个opener并安装不使用代理的处理器
            opener = urllib.request.build_opener(no_proxy_handler)
            # 设置User-Agent
            opener.addheaders = [('User-Agent', userAgentString)]
            
            # 添加较小的随机延迟，避免请求过于频繁
            time.sleep(random.uniform(0.05, 0.1))
            
            # 使用自定义opener打开URL
            with opener.open(url, timeout=5) as response:
                data = response.read().decode('utf-8')
            
            # 解析JSON响应
            response = json.loads(data)
            
            # 提取翻译结果
            if response and isinstance(response, list) and len(response) > 0 and isinstance(response[0], list):
                result = ""
                for sentence in response[0]:
                    if sentence and isinstance(sentence, list) and len(sentence) > 0:
                        result += sentence[0]
                
                failCount = 0  # 重置失败计数
                translationFailed = False  # 重置失败标志
                return result.strip()
            else:
                # 如果响应格式不符合预期
                failCount += 1
                if attempt < MAX_RETRIES - 1:
                    time.sleep(random.uniform(0.3, 0.5))  # 减少重试等待时间
                    continue
                else:
                    logHandler.log.warning(f"翻译API响应格式异常: {data[:100]}...")
                    translationFailed = True
                    return text  # 返回原文
        except Exception as e:
            failCount += 1
            logHandler.log.error(f"翻译请求失败(尝试 {attempt+1}/{MAX_RETRIES}): {str(e)}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(random.uniform(0.3, 0.5))  # 减少重试等待时间
                continue
            else:
                translationFailed = True
                return text  # 返回原文
    
    # 如果所有重试都失败
    translationFailed = True
    return text

def splitText(text):
    '''接收文字內容，按照英文標點符號或者換段符號分割，成為 2000 字元為單位的片段，以 yield 返回切割後的片段文字。'''
    splitRe = re.compile(r"[.,!?;:\n]")
    bPos = ePos = 0
    for s in splitRe.finditer(text):
        if s.end() - bPos < 2000:
            ePos = s.end()
            continue
        else:
            yield text[bPos:ePos]
            bPos = ePos
            ePos = s.end()
    yield text[bPos:]

def splitChinese(text):
    '''若 text 包含中日韓文字以及其他語言的文字，則先切割成片段，再分別傳送到谷歌進行翻譯'''
    # 优化1: 检查整个文本是否已在缓存中
    cacheResult = transCache.get(text, None)
    if cacheResult is not None:
        transCache[text] = (cacheResult[0], cacheResult[1]+1)
        return cacheResult[0]
    
    textList = []
    pos = 0
    for i in chRe.finditer(text):
        if i.start() != pos:
            textList.append(text[pos:i.start()].strip())
        textList.append(text[i.start():i.end()].strip())
        pos = i.end()
    if pos != len(text):
        textList.append(text[pos:].strip())
    
    # 筛选出非空片段
    textList = [t for t in textList if t.strip()]
    
    if len(textList) == 0:
        return text
    elif len(textList) == 1:
        return cache(textList[0])
    else:
        # 优化2: 检查所有片段是否都已经在缓存中
        allInCache = True
        cachedResults = []
        
        for segment in textList:
            if segment.strip():
                cacheResult = transCache.get(segment, None)
                if cacheResult is not None:
                    cachedResults.append(cacheResult[0])
                    # 更新使用计数
                    transCache[segment] = (cacheResult[0], cacheResult[1]+1)
                else:
                    allInCache = False
                    break
        
        # 如果所有片段都在缓存中，直接组合返回结果
        if allInCache:
            combined = ' '.join(cachedResults)
            # 将整个文本也加入缓存
            if combined:
                transCache[text] = (combined, 0)
            return combined
        
        # 如果有片段不在缓存中，使用线程池进行并行翻译
        global transResultList
        transResultList = [None] * len(textList)
        
        # 使用线程池而不是创建大量线程
        futures = []
        for i, segment in enumerate(textList):
            if segment.strip():
                futures.append(threadPool.submit(cache, segment, 1, i))
        
        # 等待所有翻译完成
        concurrent.futures.wait(futures)
        
        combined = ' '.join(t for t in transResultList if t)
        # 将整个文本也加入缓存
        if combined:
            transCache[text] = (combined, 0)
        return combined

def cache(text, method=0, index=0):
    ''' method = 0 代表原始內容沒有被分割，可以直接回傳翻譯結果。
        method = 1 代表原始內容有被分割，要把翻譯結果存放在 transResultList 裡面。index 就是存放的位置索引。
    '''
    global transCache
    if not text.strip():
        translated = text
    else:
        cacheResult = transCache.get(text, None)
        if cacheResult != None:
            transCache[text] = (cacheResult[0], cacheResult[1]+1)
            translated = cacheResult[0]
        else:
            translated = googleTranslate(text)
            if translated and translated != text:
                transCache[text] = (translated, 0)
            else:
                translated = text
    
    if method == 0:
        return translated
    else:
        global transResultList
        transResultList[index] = translated

def francisSpeak(speechSequence, *args, **kwargs):
    global lastSequence, lastTranslatedText, uiSpeak
    if uiSpeak:
        uiSpeak = False
    else:
        lastSequence = speechSequence.copy()
    if not enableTranslate:
        return originalSpeak(speechSequence, *args, **kwargs)
    
    newSequence = []
    for i in speechSequence:
        if isinstance(i, str):
            i = i.strip()
            if i == '':
                continue
            newSequence.append(splitChinese(i))
        else:
            newSequence.append(i)
    
    lastTranslatedText = ' '.join(j for j in newSequence if isinstance(j, str))
    originalSpeak(newSequence, *args, **kwargs)
    braille.handler.message(lastTranslatedText)
    
    # 使用语音通知翻译失败
    global translationFailed
    if translationFailed:
        # 使用单独的uiSpeak标志确保不递归
        translationFailed = False
        uiSpeak = True
        ui.message('翻译请求失败，使用原文显示')


def fileSizer(size):
    if size >= 1024**3:
        return '%.1f GB' % (size/1024**3)
    elif size >= 1024**2:
        return '%.1f MB' % (size/1024**2)
    elif size >= 1024:
        return '%.1f KB' % (size/1024)
    else:
        return '%d Bytes' % size


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    scriptCategory = _("谷歌翻翻看")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        global originalSpeak, toLang, transCache, fileName
        if toLang == 'Windows':
            toLang = 'zh_TW'
        originalSpeak = speech.speech.speak
        speech.speech.speak = francisSpeak
        
        self.alerted = False
        fileName = os.path.join(os.path.dirname(__file__), 'transCache %s .json' % toLang)
        if os.path.isfile(fileName):
            try:
                with codecs.open(fileName, 'r', 'utf-8') as f:
                    transCache = json.load(f)
            except Exception as e:
                logHandler.log.error(f"无法读取翻译缓存文件: {str(e)}")
                transCache = {}
        
        currentSize = sys.getsizeof(transCache)
        if currentSize > 50*1024**2:  # 将阈值从10MB提高到50MB
            self.alerted = True
        sizeText = fileSizer(currentSize)
        
        logHandler.log.info('谷歌翻翻看已啟動。目標語言為 %s 翻譯記錄佔用 %s' % (toLang, sizeText))


    def terminate(self, *args, **kwargs):
        global originalSpeak, threadPool
        speech.speech.speak = originalSpeak
        # 关闭线程池
        threadPool.shutdown(wait=False)
        
        try:
            with codecs.open(fileName, 'w', 'utf-8') as f:
                json.dump(transCache, f)
        except Exception as e:
            logHandler.log.error(f"无法保存翻译缓存文件: {str(e)}")


    def script_clipToTranslate(self, gesture):
        '''先抓取選取的文字內容，若沒有，就抓取剪貼簿的內容進行翻譯。'''
        global lastTranslatedText, uiSpeak, translationFailed
        obj1 = api.getFocusObject()
        ti = obj1.treeInterceptor
        if isinstance(ti, treeInterceptorHandler.DocumentTreeInterceptor) and not ti.passThrough:
            obj1 = ti
        # 抓一般選取
        try:
            info = obj1.makeTextInfo(textInfos.POSITION_SELECTION)
        except (RuntimeError, NotImplementedError):
            text = None
        else:
            text = info.clipboardText
        if not text:
            # 抓 NVDA 的選取
            try:
                text = api.getReviewPosition().obj._selectThenCopyRange.clipboardText
            except AttributeError:
                text = None
        if not text:
            # 抓剪貼簿
            try:
                text = api.getClipData()
            except OSError:
                pass
        if not text:
            uiSpeak = True
            ui.message('找不到要翻譯的內容。')
            return
        
        # 重置失败标志
        translationFailed = False
        
        # 先检查整个文本是否已在缓存中
        cacheResult = transCache.get(text, None)
        if cacheResult is not None:
            transCache[text] = (cacheResult[0], cacheResult[1]+1)
            lastTranslatedText = cacheResult[0]
            uiSpeak = True
            if len(lastTranslatedText) > 1500:
                ui.message(f'翻譯結果有 {len(lastTranslatedText)} 個字元，前 1500 字元的內容是： {lastTranslatedText[:1500]}')
            else:
                ui.message(lastTranslatedText)
            return
        
        lastTranslatedText = ''
        # 先以 2000 字元為單位進行分割，再送去翻譯。
        for part in splitText(text):
            if part.strip():  # 确保部分有内容
                translated = googleTranslate(part)
                if translated:
                    lastTranslatedText += translated
                    tones.beep(300, 20)
                if translationFailed:
                    uiSpeak = True
                    ui.message('翻译服务暂时不可用，请稍后再试。')
                    translationFailed = False
                    return
        
        if not lastTranslatedText and failCount >= MAX_RETRIES:
            uiSpeak = True
            ui.message('翻译服务暂时不可用，请稍后再试。')
            return
        
        # 将完整翻译结果存入缓存
        if lastTranslatedText and lastTranslatedText != text:
            transCache[text] = (lastTranslatedText, 0)
        
        uiSpeak = True
        if len(lastTranslatedText) > 1500:
            ui.message(f'翻譯結果有 {len(lastTranslatedText)} 個字元，前 1500 字元的內容是： {lastTranslatedText[:1500]}')
        else:
            ui.message(lastTranslatedText)
    script_clipToTranslate.__doc__ = _("翻譯選取的文字或者剪貼簿的內容")

    def script_sequenceToTranslate(self, gesture):
        global lastTranslatedText, uiSpeak, translationFailed
        if self.alerted:
            uiSpeak = True
            ui.message('您的記錄已經超過 50 MB ，請盡快為您的記錄瘦身。快速鍵： NVDA+Windows+J')
            self.alerted = False
        
        if not lastSequence:
            uiSpeak = True
            ui.message('没有可翻译的内容。')
            return
        
        # 重置失败标志
        translationFailed = False
        
        # 构建完整文本来检查缓存
        fullText = ' '.join(i for i in lastSequence if isinstance(i, str) and i.strip())
        if fullText:
            cacheResult = transCache.get(fullText, None)
            if cacheResult is not None:
                transCache[fullText] = (cacheResult[0], cacheResult[1]+1)
                lastTranslatedText = cacheResult[0]
                uiSpeak = True
                ui.message(lastTranslatedText)
                return
            
        resultList = []
        for i in lastSequence:
            if isinstance(i, str):
                i = i.strip()
                if i:  # 确保内容不为空
                    resultList.append(splitChinese(i))
                    if translationFailed:
                        uiSpeak = True
                        ui.message('翻译服务暂时不可用，请稍后再试。')
                        translationFailed = False
                        return
        
        if not resultList:
            uiSpeak = True
            ui.message('没有可翻译的内容。')
            return
            
        lastTranslatedText = ' '.join(x for x in resultList if x)
        
        # 将完整翻译结果存入缓存
        if lastTranslatedText and fullText and lastTranslatedText != fullText:
            transCache[fullText] = (lastTranslatedText, 0)
        
        if not lastTranslatedText and failCount >= MAX_RETRIES:
            uiSpeak = True
            ui.message('翻译服务暂时不可用，请稍后再试。')
            return
            
        uiSpeak = True
        ui.message(lastTranslatedText)
    script_sequenceToTranslate.__doc__ = _("翻譯目前語音朗讀的內容")

    def script_toggle(self, gesture):
        global enableTranslate, uiSpeak
        if enableTranslate:
            enableTranslate = False
            uiSpeak = True
            ui.message('關閉自動翻譯。')
        else:
            uiSpeak = True
            ui.message('開啟自動翻譯。')
            enableTranslate = True
            if self.alerted:
                originalSpeak('您的記錄已經超過 50 MB ，請盡快為您的記錄瘦身。快速鍵： NVDA+Windows+J')
                braille.handler.message('您的記錄已經超過 50 MB ，請盡快為您的記錄瘦身。快速鍵： NVDA+Windows+J')
                self.alerted = False
    script_toggle.__doc__ = _("自動翻譯開關")

    def script_clearTransCache(self, gesture):
        global transCache
        transCache = {}
        if os.path.isfile(fileName):
            try:
                os.unlink(fileName)
                ui.message('清除翻译记录成功。')
            except Exception as e:
                logHandler.log.error(f"无法删除缓存文件: {str(e)}")
                ui.message('清除翻译记录时发生错误。')
        else:
            ui.message('清除翻译记录。')
    script_clearTransCache.__doc__ = _("清除所有翻譯記錄")


    def script_miniTransCache(self, gesture):
        global transCache
        originalSize = sys.getsizeof(transCache)
        temp1 = list(transCache.keys())
        if len(temp1) <= 1000:  # 从100增加到1000
            ui.message('您的記錄已經很瘦了！不需要再瘦身。')
            return
        ui.message('處理中，請稍等。')
        
        # 基于使用频率对缓存进行排序
        cache_items = [(k, v[0], v[1]) for k, v in transCache.items()]
        # 按使用频率降序排序
        sorted_items = sorted(cache_items, key=lambda x: x[2], reverse=True)
        
        # 只保留前1000个常用条目，比之前的100多10倍
        transCache = {item[0]: (item[1], 0) for item in sorted_items[:1000]}
        
        try:
            with codecs.open(fileName, 'w', 'utf-8') as f:
                json.dump(transCache, f)
        except Exception as e:
            logHandler.log.error(f"无法保存翻译缓存文件: {str(e)}")
            ui.message('保存瘦身结果时发生错误。')
            return
            
        currentSize = sys.getsizeof(transCache)
        minusSize = originalSize - currentSize
        infoText = '恭喜您！成功瘦身。已縮減 %s 目前剩餘 %s' % (fileSizer(minusSize), fileSizer(currentSize))
        ui.message(infoText)
    script_miniTransCache.__doc__ = _("為翻譯記錄瘦身")


    def script_readOrCopy(self, gesture):
        global uiSpeak
        
        if not lastTranslatedText:
            uiSpeak = True
            ui.message('没有可读取的翻译结果。')
            return
            
        uiSpeak = True
        if len(lastTranslatedText) > 1500:
            ui.message(f'翻譯結果有 {len(lastTranslatedText)} 個字元，前 1500 字元的內容是： {lastTranslatedText[:1500]}')
        else:
            ui.message(lastTranslatedText)
        if scriptHandler.getLastScriptRepeatCount() == 1:
            try:
                api.copyToClip(lastTranslatedText)
                speech.cancelSpeech()
                uiSpeak = True
                ui.message('已複製到剪貼簿')
            except Exception as e:
                logHandler.log.error(f"无法复制到剪贴板: {str(e)}")
                uiSpeak = True
                ui.message('复制到剪贴板时发生错误')
    script_readOrCopy.__doc__ = _("朗讀最後一個翻譯結果。快按兩下複製到剪貼簿")

    __gestures={
        "kb:NVDA+control+J":"clipToTranslate",
        "kb:NVDA+J":"sequenceToTranslate",
        "kb:NVDA+shift+control+J":"toggle",
        "kb:NVDA+alt+J":"readOrCopy",
        "kb:NVDA+windows+J":"miniTransCache",
        "kb:NVDA+shift+windows+J":"clearTransCache"
    }